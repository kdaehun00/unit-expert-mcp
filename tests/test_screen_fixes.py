"""Behaviour of the control pages that came out of browser testing: what the
inspection log names as injected, how the screens read it, what the pages do
while a config has an error, and the small reading aids around them."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mcp_test_support import (
    CONVERT_ARGS,
    HttpServerTestCase,
    legacy_initialize,
    legacy_request,
    modern_call,
    modern_headers,
    modern_request,
)
from test_inspect_page import InspectPageScriptTest, exchange
from test_scenario_page import NODE, parse
from test_ui_logic import UiCoreTestCase
from unit_expert_mcp.server import (
    INSPECT_LOG,
    PREVIEW_SOURCE,
    encode_config_token,
    handle_mcp_exchange,
    inspect_snapshot,
    issue_access_token,
    normalize_config,
    render_inspect_page,
    render_scenario_page,
    render_send_page,
    scenario_action,
    scenario_meta,
)

BOTH_ACCEPT = "application/json, text/event-stream"


def injected(payload: dict[str, Any], config: dict[str, Any], headers: dict[str, str] | None = None) -> list[str]:
    return handle_mcp_exchange(payload, config=config, headers=headers)["injected"]


class InjectedLabelTest(unittest.TestCase):
    """Options that change a response are named in the reply's ``injected``."""

    def test_default_config_names_nothing(self) -> None:
        payload, headers = modern_call("convert_length", CONVERT_ARGS)
        for request, request_headers in (
            (legacy_initialize(), None),
            (legacy_request("tools/list"), None),
            (legacy_request("tools/call", name="convert_length", arguments=CONVERT_ARGS), None),
            (modern_request("server/discover"), modern_headers("server/discover")),
            (modern_request("tools/list"), modern_headers("tools/list")),
            (payload, headers),
        ):
            with self.subTest(method=request["method"], modern=request_headers is not None):
                self.assertEqual(injected(request, {}, request_headers), [])

    def test_tools_list_modes_and_tool_definitions(self) -> None:
        cases = (
            ({"toolsList": {"mode": "json-rpc-error"}}, ["toolsList.mode=json-rpc-error"]),
            ({"toolsList": {"mode": "empty"}}, ["toolsList.mode=empty"]),
            ({"toolsList": {"mode": "null"}}, ["toolsList.mode=null"]),
            ({"toolsList": {"mode": "too-many"}}, ["toolsList.mode=too-many"]),
            (
                {"toolsList": {"mode": "too-many", "tooManyCount": 30, "duplicateToolName": True}},
                ["toolsList.mode=too-many", "toolsList.tooManyCount=30", "toolsList.duplicateToolName"],
            ),
            ({"toolsList": {"duplicateToolName": True}}, ["toolsList.duplicateToolName"]),
            ({"toolErrors": ["missing-name", "long-description"]}, ["toolErrors=missing-name,long-description"]),
            (
                {"toolsList": {"mode": "empty", "pageSize": 2}},
                ["toolsList.mode=empty", "toolsList.pageSize=2"],
            ),
        )
        for config, expected in cases:
            with self.subTest(config=config):
                self.assertEqual(injected(legacy_request("tools/list"), config), expected)
                modern = injected(modern_request("tools/list"), config, modern_headers("tools/list"))
                self.assertEqual(modern, expected)

    def test_modern_only_tools_list_options(self) -> None:
        def modern(config: dict[str, Any]) -> list[str]:
            return injected(modern_request("tools/list"), config, modern_headers("tools/list"))

        self.assertEqual(modern({"modern": {"testTools": False}}), ["modern.testTools=false"])
        self.assertEqual(
            modern({"toolsList": {"cacheTtlMs": 60000, "cacheScope": "public"}}),
            ["toolsList.cacheTtlMs=60000", "toolsList.cacheScope=public"],
        )
        # The legacy answer has neither test tools nor cache hints.
        legacy_config = {"modern": {"testTools": False}, "toolsList": {"cacheTtlMs": 60000}}
        self.assertEqual(injected(legacy_request("tools/list"), legacy_config), [])
        lenient = {"strict2026": False, "toolsList": {"mode": "empty"}}
        self.assertEqual(modern(lenient), ["strict2026=false", "toolsList.mode=empty"])

    def test_hidden_test_tool_and_the_status_of_invalid_params(self) -> None:
        payload, headers = modern_call("echo_header_params", {})
        self.assertEqual(injected(payload, {"modern": {"testTools": False}}, headers), ["modern.testTools=false"])
        unknown, unknown_headers = modern_call("no_such_tool", {})
        self.assertEqual(injected(unknown, {"modern": {"testTools": False}}, unknown_headers), [])
        self.assertEqual(
            injected(unknown, {"modern": {"invalidParamsHttpStatus": 400}}, unknown_headers),
            ["modern.invalidParamsHttpStatus=400"],
        )

    def test_param_header_let_through(self) -> None:
        payload, headers = modern_call("echo_header_params", {}, param_headers={"Mcp-Param-Region": "kr"})
        lenient = {"modern": {"rejectUnexpectedParamHeader": False}}
        self.assertEqual(injected(payload, lenient, headers), ["modern.rejectUnexpectedParamHeader=false"])
        clean, clean_headers = modern_call("echo_header_params", {})
        self.assertEqual(injected(clean, lenient, clean_headers), [])

    def test_initialize_version_and_discover_cache(self) -> None:
        fixed = {"initialize": {"protocolVersion": "2024-11-05"}}
        self.assertEqual(injected(legacy_initialize(), fixed), ["initialize.protocolVersion=2024-11-05"])
        self.assertEqual(
            injected(legacy_initialize(version="2025-06-18"), {"initialize": {"protocolVersionEnabled": False}}),
            ["initialize.protocolVersionEnabled=false"],
        )
        # Negotiating the version the default config answers with changes nothing.
        self.assertEqual(injected(legacy_initialize(), {"initialize": {"protocolVersionEnabled": False}}), [])
        self.assertEqual(
            injected(
                modern_request("server/discover"),
                {"discover": {"cacheTtlMs": 5000}},
                modern_headers("server/discover"),
            ),
            ["discover.cacheTtlMs=5000"],
        )

    def test_the_mcp_response_is_the_same_with_and_without_labels(self) -> None:
        """Naming an option never changes status, headers or body."""
        config = {"toolsList": {"mode": "empty"}}
        reply = handle_mcp_exchange(legacy_request("tools/list"), config=config)
        self.assertEqual((reply["status"], reply["headers"]), (200, {}))
        self.assertEqual(reply["body"], {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})


class InjectedOnTheWireTest(HttpServerTestCase):
    def last(self) -> dict[str, Any]:
        return inspect_snapshot()[0]

    def test_oauth_refusals_name_the_mode(self) -> None:
        self.post(legacy_request("tools/list"), config={"auth": {"mode": "oauth"}})
        self.assertEqual((self.last()["status"], self.last()["injected"]), (401, ["auth.mode=oauth"]))
        call = legacy_request("tools/call", name="convert_length", arguments=CONVERT_ARGS)
        self.post(call, config={"auth": {"mode": "mixed-oauth"}})
        self.assertEqual((self.last()["status"], self.last()["injected"]), (401, ["auth.mode=mixed-oauth"]))
        # A public request under mixed-oauth is not a made-up response.
        self.post(legacy_request("tools/list"), config={"auth": {"mode": "mixed-oauth"}})
        self.assertEqual((self.last()["status"], self.last()["injected"]), (200, []))

    def test_scope_refusal_names_both_options(self) -> None:
        token = issue_access_token({"sub": "u", "client_id": "c", "scope": "other"})
        self.post(
            legacy_request("tools/list"),
            config={"auth": {"mode": "oauth", "enforceScope": True}},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(
            (self.last()["status"], self.last()["injected"]), (403, ["auth.mode=oauth", "auth.enforceScope"])
        )

    def test_get_oauth_refusal_names_the_mode(self) -> None:
        path = "/mcp?cfg=" + encode_config_token(normalize_config({"auth": {"mode": "oauth"}}))
        self.request("GET", path, headers={"Accept": "text/event-stream"})
        self.assertEqual((self.last()["status"], self.last()["injected"]), (401, ["auth.mode=oauth"]))

    def test_the_server_delay_is_named_after_the_other_options(self) -> None:
        config = {"server": {"delayEnabled": True, "delaySeconds": 0.05}, "toolsList": {"mode": "empty"}}
        status, _, body, _ = self.post(legacy_request("tools/list"), config=config)
        self.assertEqual((status, body["result"]["tools"]), (200, []))
        self.assertEqual(self.last()["injected"], ["toolsList.mode=empty", "server.delaySeconds=0.05"])
        # Not named on a request the delay does not touch.
        self.post(legacy_request("tools/list"))
        self.assertEqual(self.last()["injected"], [])

    def test_favicon_is_an_empty_answer(self) -> None:
        before = len(inspect_snapshot())
        status, _, raw = self.request("GET", "/favicon.ico")
        self.assertEqual((status, raw), (204, b""))
        self.assertEqual(len(inspect_snapshot()), before)


class TokenTextTest(unittest.TestCase):
    def test_decode_reads_every_form_the_page_copies(self) -> None:
        config = normalize_config({"serverEra": "modern"})
        token = encode_config_token(config)
        for text in (
            token,
            f"cfg={token}",
            f"cfg:{token}",
            f"http://localhost:8000/mcp?cfg={token}",
            f"X-MCP-Test-Scenario: cfg:{token}",
            f"  x-mcp-test-scenario:cfg:{token}  ",
        ):
            with self.subTest(text=text):
                self.assertEqual(scenario_action({"action": "decode", "token": text})["token"], token)


class ScreenLogicTest(UiCoreTestCase):
    def entry(self, **fields: Any) -> dict[str, Any]:
        return exchange(1, **fields)

    def test_token_from_the_header_line(self) -> None:
        cases = {
            "X-MCP-Test-Scenario: cfg:eyJhIjoxfQ": "eyJhIjoxfQ",
            "x-mcp-test-scenario:cfg:eyJhIjoxfQ": "eyJhIjoxfQ",
            "X-MCP-Test-Scenario: auth-401": "auth-401",
        }
        results = self.run_calls([["tokenFromText", text] for text in cases])
        self.assertEqual([result["value"] for result in results], list(cases.values()))

    def test_request_mode(self) -> None:
        cases = (
            (self.entry(), "2025"),
            (self.entry(era="2026"), "2026"),
            (self.entry(httpMethod="OPTIONS", method=None), ""),
            (self.entry(httpMethod="GET", method=None), ""),
            (self.entry(httpMethod="GET", method=None, era="2026"), "2026"),
            (self.entry(method=None, requestBody="{not json"), ""),
        )
        results = self.run_calls([["requestMode", entry] for entry, _ in cases])
        self.assertEqual([result["value"] for result in results], [expected for _, expected in cases])
        # The legacy filter leaves out what cannot be told.
        shown = self.call("filterExchanges", [entry for entry, _ in cases], {"era": "2025"})
        self.assertEqual(len(shown), 1)

    def test_a_response_that_was_only_delayed_keeps_its_kind(self) -> None:
        refused = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32020, "message": "x"}}
        cases = (
            (self.entry(injected=["server.delaySeconds=5"]), "ok"),
            (self.entry(injected=["toolCall.delaySeconds=0.2"], status=400, responseBody=refused), "rejected"),
            (self.entry(injected=["toolsList.mode=empty", "server.delaySeconds=5"]), "injected"),
            (self.entry(injected=["discover.mode=sse", "discover.delaySeconds=1"]), "injected"),
        )
        results = self.run_calls(
            [["classifyExchange", entry, self.meta["errorCodes"]] for entry, _ in cases]
        )
        self.assertEqual([result["value"]["kind"] for result in results], [kind for _, kind in cases])
        self.assertEqual(results[0]["value"]["delays"], ["server.delaySeconds=5"])
        self.assertEqual(results[2]["value"]["delays"], ["server.delaySeconds=5"])
        # "에러만" does not count a delayed normal answer.
        self.assertEqual(self.call("filterExchanges", [cases[0][0]], {"errorsOnly": True}), [])

    def test_search_finds_what_the_list_shows(self) -> None:
        refused = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32020, "message": "x"}}
        entries = [
            self.entry(),
            exchange(2, status=400, responseBody=refused),
            exchange(3, injected=["toolsList.mode=empty"]),
        ]
        codes = self.meta["errorCodes"]
        results = self.run_calls(
            [
                ["filterExchanges", entries, {"text": "HeaderMismatch", "errorCodes": codes}],
                ["filterExchanges", entries, {"text": "-32020 headermismatch", "errorCodes": codes}],
                ["filterExchanges", entries, {"text": "서버가 거절", "errorCodes": codes}],
                ["filterExchanges", entries, {"text": "넣은 에러", "errorCodes": codes}],
                ["filterExchanges", entries, {"text": "정상", "errorCodes": codes}],
            ]
        )
        self.assertEqual(
            [[entry["seq"] for entry in result["value"]] for result in results], [[2], [2], [2], [3], [1]]
        )

    def test_filter_blockers_name_the_filters(self) -> None:
        preview = exchange(2, source=PREVIEW_SOURCE)
        options = exchange(3, httpMethod="OPTIONS", method=None, requestBody=None, responseBody=None)
        filters = {"hideScreen": True, "hideOptions": True, "text": "tools/list", "kind": "rejected"}
        results = self.run_calls(
            [
                ["filterBlockers", self.entry(), {"hideScreen": True, "hideOptions": True}],
                ["filterBlockers", preview, {"hideScreen": True, "hideOptions": True}],
                ["filterBlockers", options, {"hideScreen": True, "hideOptions": True}],
                ["filterBlockers", preview, filters],
                ["filterBlockers", options, filters],
            ]
        )
        self.assertEqual(
            [result["value"] for result in results],
            [[], ["hideScreen"], ["hideOptions"], ["hideScreen", "kind"], ["hideOptions", "kind", "text"]],
        )

    def test_config_error_names_the_option(self) -> None:
        cases = (
            (
                "mcp.identifier must be 128 characters or fewer",
                "mcp.identifier",
                "설정 오류: 서버 이름 (mcp.identifier) — mcp.identifier must be 128 characters or fewer",
            ),
            (
                "rawResponses[0].status must be an integer between 200 and 599",
                "rawResponses",
                "설정 오류: 직접 지정한 응답 (rawResponses[0].status) — "
                "rawResponses[0].status must be an integer between 200 and 599",
            ),
            ("unknown tool error 'x'", None, "설정 오류: unknown tool error 'x'"),
            ("serverEraX must be y", None, "설정 오류: serverEraX must be y"),
        )
        results = self.run_calls([["describeConfigError", message, self.schema] for message, _, _ in cases])
        for result, (_, path, text) in zip(results, cases):
            value = result["value"]
            self.assertEqual(value["option"]["path"] if value["option"] else None, path)
            self.assertEqual(value["text"], text)

    def test_option_search_finds_the_server_generation(self) -> None:
        option = self.options["serverEra"]
        results = self.run_calls(
            [["matchOptionSearch", option, query] for query in ("serverEra", "지원 스펙", "2025 방식만", "세션")]
        )
        self.assertEqual([result["value"] for result in results], [True, True, True, False])


class PageWordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario = render_scenario_page()
        cls.inspect = render_inspect_page()
        cls.meta = scenario_meta()

    def test_cli_on_the_screen_uses_python3(self) -> None:
        self.assertIn('"cliPrefix": "PYTHONPATH=src python3 -m unit_expert_mcp.client"', self.scenario)
        # The whole command is on the screen, never a shortened one.
        self.assertNotIn("shortCommand", self.scenario)

    def test_matrix_cells_are_named_in_korean(self) -> None:
        box = parse(self.scenario).by_id("compatBox")["text"]
        for text in ("2025 방식 요청", "2026 방식 요청", "서버: 2025 방식만", "서버: 2026-07-28만", "그대로 동작"):
            with self.subTest(text=text):
                self.assertIn(text, box)
        for text in (
            "2025 방식 요청 → 2025 방식만 지원하는 서버",
            "2025 방식 요청 → 2026-07-28만 지원하는 서버",
            "2026 방식 요청 → 2025 방식만 지원하는 서버",
            "2026 방식 요청 → 2026-07-28만 지원하는 서버",
        ):
            with self.subTest(text=text):
                self.assertIn(f'aria-label="{text}: ', self.scenario)
        self.assertNotIn("modern client → modern server", box)

    def test_one_name_per_side(self) -> None:
        """Request side: 2025 방식 / 2026 방식. Server side: 지원 스펙
        (2025 방식만 / 2026-07-28만 / 둘 다). The older names are gone from
        what the three screens show."""
        for page in (self.scenario, self.inspect, render_send_page()):
            self.assertNotIn("모던", page)
            for old_name in ("레거시", "서버 세대", "cfg 토큰", "주입", "검증 거부", "미리보기"):
                with self.subTest(old_name=old_name):
                    self.assertNotIn(old_name, page)
        for old_name in ("2026 무상태", "레거시 핸드셰이크"):
            for page in (self.scenario, self.inspect):
                self.assertNotIn(old_name, page)

    def test_one_word_for_tool_and_body(self) -> None:
        """툴 (not 도구) and 바디 (not 본문), on the pages and in /scenario/meta."""
        meta_text = json.dumps(self.meta, ensure_ascii=False)
        for word in ("도구", "본문"):
            for name, text in (("scenario", self.scenario), ("inspect", self.inspect), ("meta", meta_text)):
                with self.subTest(word=word, where=name):
                    self.assertNotIn(word, text)

    def test_inspect_says_how_headers_are_recorded(self) -> None:
        notice = re.search(r'<p class="caution" id="caution" role="note">([^<]*)</p>', self.inspect)
        self.assertIsNotNone(notice)
        self.assertEqual(
            notice.group(1),
            "최근 200건을 모든 사용자가 함께 봅니다. 인증 헤더 값은 가려서 기록하고, 바디는 그대로 남습니다.",
        )

    def test_the_cli_prefix_is_defined_once(self) -> None:
        self.assertNotIn("cliPrefixInMeta", self.scenario)
        for preset in self.meta["presets"]:
            if preset["category"] == "matrix":
                self.assertTrue(preset["cli"].startswith("PYTHONPATH=src python3 -m unit_expert_mcp.client "))

    def test_descriptions_end_politely(self) -> None:
        plain_endings = ("한다", "준다", "는다", "뺀다", "된다", "없다", "있다", "꾼다", "춘다", "진다", "인다")
        texts = [option["description"] for option in self.meta["options"]]
        texts += [preset["description"] for preset in self.meta["presets"]]
        texts += [preset["expect"] for preset in self.meta["presets"] if "expect" in preset]
        for text in texts:
            for sentence in text.replace("(", ". ").replace(")", ". ").split(". "):
                with self.subTest(sentence=sentence):
                    self.assertFalse(sentence.strip().rstrip(".").endswith(plain_endings), sentence)

    def test_load_token_and_copy_wording(self) -> None:
        dom = parse(self.scenario)
        self.assertEqual(
            dom.by_id("loadToken")["attrs"]["placeholder"], "MCP 주소, 설정 코드, 또는 설정 JSON을 붙여 넣으세요"
        )
        self.assertEqual(dom.by_id("loadToken")["tag"], "textarea")
        self.assertIn('"복사했습니다: " + label', self.scenario)
        self.assertNotIn("을(를)", self.scenario)
        self.assertNotIn("오른쪽", self.scenario)
        # Everything but the address is under "더 보기", each with what it copies.
        folded = dom.by_id("moreBox")["text"]
        for label in ("설정 코드", "요청 헤더로", "테스트 명령", "curl", "설정 JSON", "설정 불러오기"):
            self.assertIn(label, folded)

    def test_inspect_page_holds_what_the_server_keeps(self) -> None:
        size = INSPECT_LOG.maxlen
        self.assertIn(f'"logSize": {size}', self.inspect)
        self.assertIn("const MAX_ROWS = META.logSize;", self.inspect)
        self.assertIn(f"최근 {size}건을 모든 사용자가 함께 봅니다.", self.inspect)
        self.assertIn('"echo_header_params": [{"header": "Region", "argument": "region"}', self.inspect)


SCENARIO_STUB = r"""
const fs = require("fs");
const [scriptsPath, planPath] = process.argv.slice(2);
const scripts = JSON.parse(fs.readFileSync(scriptsPath, "utf8"));
const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
const elements = {};
const requests = [];
const documentListeners = {};
let uid = 0;
let focused = null;

function matches(node, selector) {
  return selector.charAt(0) === "." && String(node.className).split(/\s+/).indexOf(selector.slice(1)) >= 0;
}
function collect(node, selector, out) {
  node.children.forEach((child) => {
    if (matches(child, selector)) out.push(child);
    collect(child, selector, out);
  });
  return out;
}
function fakeElement(id) {
  const node = {
    id: id, uid: ++uid, value: "", textContent: "", hidden: false, disabled: false, checked: false, href: "",
    className: "", title: "", dataset: {}, style: {}, open: false, children: [], parent: null, listeners: {}, attrs: {},
    addEventListener(type, handler) { (node.listeners[type] = node.listeners[type] || []).push(handler); },
    setAttribute(name, value) { node.attrs[name] = String(value); },
    append(...nodes) { nodes.forEach((child) => { child.remove(); child.parent = node; node.children.push(child); }); },
    insertBefore(child, before) {
      child.remove();
      child.parent = node;
      const index = before ? node.children.indexOf(before) : -1;
      if (index < 0) node.children.push(child); else node.children.splice(index, 0, child);
    },
    get firstChild() { return node.children[0] || null; },
    get nextSibling() {
      if (!node.parent) return null;
      return node.parent.children[node.parent.children.indexOf(node) + 1] || null;
    },
    remove() {
      if (!node.parent) return;
      node.parent.children = node.parent.children.filter((child) => child !== node);
      node.parent = null;
    },
    focus() { focused = node; }, select() {}, scrollIntoView() {},
    querySelector() { return null; },
    querySelectorAll(selector) { return collect(node, selector, []); },
    closest() { return null; }
  };
  node.classList = {
    toggle() {}, add() {}, remove() {},
    contains(name) { return String(node.className).split(/\s+/).indexOf(name) >= 0; }
  };
  return node;
}
global.document = {
  getElementById(id) { return elements[id] || (elements[id] = fakeElement(id)); },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  createElement(tag) { return fakeElement(tag); },
  addEventListener(type, handler) { (documentListeners[type] = documentListeners[type] || []).push(handler); },
  body: fakeElement("body")
};
global.location = { origin: "http://localhost:8000", host: "localhost:8000", pathname: "/scenario", search: "", hash: "" };
global.history = { replaceState(state, title, url) { const i = url.indexOf("#"); location.hash = i < 0 ? "" : url.slice(i); } };
global.navigator = {};
global.window = { addEventListener() {}, confirm() { return true; }, isSecureContext: false };
global.fetch = async (url, init) => {
  const payload = JSON.parse(init.body);
  requests.push({ url: url, action: payload.action || null });
  if (url !== "/scenario") throw new Error("unexpected request to " + url);
  let body;
  if (payload.action === "decode") body = plan.decode;
  else if (payload.config.rawResponses && payload.config.rawResponses.length) body = { ok: false, error: plan.serverError };
  else body = { ok: true, config: payload.config, token: null };
  return { status: 200, json: async () => body };
};
function wait(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }
function el(id) { return document.getElementById(id); }
function fire(type, target) {
  (documentListeners[type] || []).forEach((handler) => handler({ type: type, target: target }));
}
function button(fields) {
  const node = fakeElement("button");
  Object.assign(node.dataset, fields.dataset || {});
  node.className = fields.className || "";
  node.closest = () => node;
  return node;
}
function snapshot() {
  return {
    url: el("mcpUrl").value,
    urlError: el("urlError").textContent,
    jumpHidden: el("urlErrorJump").hidden,
    jump: el("urlErrorJump").textContent,
    inspect: el("inspectLink").href,
    hash: location.hash,
    token: el("copy-token").textContent,
    curl: el("copy-curlLegacy").textContent,
    send: el("sendLink").href,
    chips: el("chips").children.map((chip) => ({ uid: chip.uid, path: chip.dataset.chip, text: chip.children[0].textContent })),
    resetAllDisabled: el("resetAll").disabled,
    presetDetail: el("presetDetail").textContent,
    copyStatus: el("copyStatus").textContent,
    focused: focused ? (focused.id === "button" ? "chip:" + focused.dataset.reset : focused.id) : null,
    requests: requests.map((request) => request.action)
  };
}
(async () => {
  for (const source of scripts) (0, eval)(source);
  const out = {};
  for (const step of plan.steps) {
    if (step.wait) await wait(step.wait);
    else if (step.preset) fire("click", button({ dataset: { preset: step.preset } }));
    else if (step.load) { el("loadToken").value = step.load; el("loadTokenButton").listeners.click[0](); }
    else if (step.removeChip) {
      const chip = el("chips").children.filter((item) => item.dataset.chip === step.removeChip)[0];
      const target = chip.children[1];
      target.closest = () => target;
      fire("click", target);
    }
    else if (step.click) el(step.click).listeners.click[0]();
    else if (step.copy) {
      const target = button({ dataset: { copy: step.copy, copyLabel: "주소" } });
      document.execCommand = () => true;
      fire("click", target);
    }
    else if (step.pointer) fire(step.pointer, null);
    else if (step.snapshot) out[step.snapshot] = snapshot();
    await wait(5);
  }
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exit(1); });
"""

SERVER_ERROR = "rawResponses[0].status must be an integer between 200 and 599"


@unittest.skipUnless(NODE, "node is not installed")
class ScenarioPageBehaviourTest(unittest.TestCase):
    """The /scenario script on a stand-in DOM that keeps chips and listeners."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.workdir.name)
        (cls.root / "stub.js").write_text(SCENARIO_STUB, encoding="utf-8")
        (cls.root / "scripts.json").write_text(json.dumps(parse(render_scenario_page()).scripts), encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workdir.cleanup()

    def run_plan(self, steps: list[dict[str, Any]], decode: dict[str, Any] | None = None) -> dict[str, Any]:
        plan = {"steps": steps, "decode": decode, "serverError": SERVER_ERROR}
        (self.root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        done = subprocess.run(
            [NODE, str(self.root / "stub.js"), str(self.root / "scripts.json"), str(self.root / "plan.json")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_preset_button_is_a_toggle(self) -> None:
        token = encode_config_token(normalize_config({"toolsList": {"mode": "empty"}}))
        result = self.run_plan(
            [
                {"preset": "tools-list-empty"},
                {"snapshot": "pending"},
                {"wait": 400},
                {"snapshot": "on"},
                {"preset": "tools-list-empty"},
                {"wait": 400},
                {"snapshot": "off"},
            ]
        )
        on, off = result["on"], result["off"]
        self.assertEqual(on["url"], f"http://localhost:8000/mcp?cfg={token}")
        self.assertEqual([chip["path"] for chip in on["chips"]], ["toolsList.mode"])
        self.assertEqual(on["presetDetail"], "넣었습니다: 툴이 하나도 없는 목록을 줍니다")
        self.assertEqual(off["url"], "http://localhost:8000/mcp")
        self.assertEqual(off["chips"], [])
        self.assertEqual(off["presetDetail"], "뺐습니다: tools/list 빈 목록")
        self.assertEqual(off["hash"], "")
        # Until the server has answered, the link and #cfg keep the last valid token.
        pending = result["pending"]
        self.assertEqual((pending["hash"], pending["inspect"], pending["send"]), ("", "/inspect", "/send"))
        self.assertEqual((on["hash"], on["inspect"]), (f"#cfg={token}", f"/inspect?cfg={token}"))
        # The send screen opens with the same address.
        self.assertEqual(on["send"], f"/send?cfg={token}")

    def test_chips_are_the_same_nodes_and_focus_moves_on(self) -> None:
        config = normalize_config({"serverEra": "legacy", "toolCall": {"mode": "is-error"}})
        token = encode_config_token(config)
        result = self.run_plan(
            [
                {"load": token},
                {"snapshot": "loaded"},
                {"preset": "tools-list-paged"},
                {"wait": 400},
                {"snapshot": "three"},
                {"removeChip": "serverEra"},
                {"snapshot": "first"},
                {"removeChip": "toolCall.mode"},
                {"snapshot": "last"},
                {"removeChip": "toolsList.pageSize"},
                {"snapshot": "none"},
            ],
            decode={"ok": True, "config": config, "token": token},
        )
        loaded, three = result["loaded"], result["three"]
        self.assertEqual([chip["path"] for chip in loaded["chips"]], ["serverEra", "toolCall.mode"])
        # A token the server has just decoded is not sent back for validation.
        self.assertEqual(loaded["requests"], ["decode"])
        self.assertEqual(loaded["hash"], f"#cfg={token}")
        self.assertEqual(
            [chip["path"] for chip in three["chips"]], ["serverEra", "toolsList.pageSize", "toolCall.mode"]
        )
        kept = {chip["path"]: chip["uid"] for chip in loaded["chips"]}
        self.assertEqual({chip["path"]: chip["uid"] for chip in three["chips"] if chip["path"] in kept}, kept)
        # The chip that takes the removed one's place gets the focus, then the
        # last chip, then the URL field.
        self.assertEqual(result["first"]["focused"], "chip:toolsList.pageSize")
        self.assertEqual(result["last"]["focused"], "chip:toolsList.pageSize")
        self.assertEqual(result["none"]["focused"], "mcpUrl")
        self.assertTrue(result["none"]["resetAllDisabled"])

    def test_a_server_error_blocks_the_url_and_does_not_flicker(self) -> None:
        good = normalize_config({"serverEra": "legacy"})
        good_token = encode_config_token(good)
        bad = json.loads(json.dumps(good))
        bad["rawResponses"] = [{"method": "tools/list", "status": 99}]
        result = self.run_plan(
            [
                {"load": good_token},
                {"snapshot": "good"},
                {"load": "bad"},
                {"wait": 400},
                {"snapshot": "error"},
                {"preset": "tools-list-empty"},
                {"snapshot": "editing"},
                {"wait": 400},
                {"snapshot": "still"},
                {"click": "resetAll"},
                {"snapshot": "reset"},
            ],
            decode={"ok": True, "config": bad},
        )
        # (The stand-in answers every decode with the bad config, so "good" is it too.)
        error = result["error"]
        self.assertEqual(error["url"], "")
        self.assertEqual(
            error["urlError"],
            "설정 오류: 직접 지정한 응답 (rawResponses[0].status) — " + SERVER_ERROR
            + "\n오류를 고치면 주소가 만들어집니다.",
        )
        self.assertFalse(error["jumpHidden"])
        self.assertEqual(error["jump"], "오류가 난 곳으로 이동: 직접 지정한 응답")
        self.assertEqual((error["hash"], error["inspect"]), ("", "/inspect"))
        self.assertEqual(error["token"], "오류를 고치면 만들어집니다.")
        self.assertEqual(error["curl"], "오류를 고치면 만들어집니다.")
        # While the next change is being checked the error stays where it is.
        for name in ("editing", "still"):
            with self.subTest(name=name):
                self.assertEqual(result[name]["urlError"], error["urlError"])
                self.assertEqual(result[name]["url"], "")
        reset = result["reset"]
        self.assertEqual((reset["url"], reset["urlError"]), ("http://localhost:8000/mcp", ""))
        self.assertTrue(reset["jumpHidden"])
        self.assertEqual(reset["focused"], "mcpUrl")

    def test_copy_notice_names_what_was_copied(self) -> None:
        result = self.run_plan([{"copy": "url"}, {"snapshot": "copied"}])
        self.assertEqual(result["copied"]["copyStatus"], "복사했습니다: 주소")


class InspectPageBehaviourTest(InspectPageScriptTest):
    """/inspect on the stand-in DOM of tests/test_inspect_page.py."""

    STUB = InspectPageScriptTest.STUB.replace(
        "    emptyText: el(\"emptyText\").textContent,",
        "    emptyText: el(\"emptyText\").textContent,\n"
        "    emptyShowHidden: el(\"emptyShow\").hidden,\n"
        "    emptyShow: el(\"emptyShow\").textContent,\n"
        "    newBannerWhy: el(\"newBannerWhy\").textContent,",
    )

    def test_a_cfg_whose_only_requests_came_from_a_screen(self) -> None:
        token = "eyJ0b29sc0xpc3QiOnsicGFnZVNpemUiOjJ9fQ"
        log = [
            exchange(1),
            exchange(2, cfg=token, configSource="url", source=PREVIEW_SOURCE),
            exchange(3, cfg=token, configSource="url", source=PREVIEW_SOURCE),
        ]
        result = self.run_plan(
            [{"snapshot": "empty"}, {"click": "emptyShow"}, {"snapshot": "shown"}],
            log=log,
            search=f"?cfg={token}",
        )
        empty, shown = result["empty"], result["shown"]
        self.assertEqual(self.shown(empty), [])
        self.assertFalse(empty["emptyHidden"])
        self.assertIn("요청 보내기 화면에서 보낸 요청 2건만 있고, 지금은 숨겨져 있습니다.", empty["emptyText"])
        self.assertNotIn("이 주소로 온 요청이 아직 없습니다", empty["emptyText"])
        self.assertFalse(empty["emptyShowHidden"])
        self.assertEqual(empty["emptyShow"], "보기")
        self.assertEqual(self.shown(shown), [3, 2])
        self.assertFalse(shown["hideScreen"])
        self.assertTrue(shown["hideOptions"])
        self.assertEqual(shown["search"], f"?cfg={token}&screen=1")

    def test_the_empty_state_names_the_filters_and_releases_only_them(self) -> None:
        result = self.run_plan(
            [
                {"set": {"id": "fKind", "value": "raw"}},
                {"snapshot": "none"},
                {"click": "emptyShow"},
                {"snapshot": "released"},
            ]
        )
        none = result["none"]
        self.assertIn(
            "필터에 맞는 요청이 없습니다. 5건이 가려져 있습니다: "
            "결과 5건, 요청 보내기 화면에서 보낸 요청 숨기기 1건, OPTIONS 숨기기 1건",
            none["emptyText"],
        )
        self.assertEqual(none["emptyShow"], "가린 필터 풀기")
        released = result["released"]
        self.assertEqual(self.shown(released), [5, 4, 3, 2, 1])
        self.assertEqual(released["search"], "?screen=1&options=1")

    def test_the_new_request_banner_says_which_filter_and_turns_it_off(self) -> None:
        result = self.run_plan(
            [
                {"add": [exchange(6, source="inspect-console")]},
                {"tick": 1},
                {"snapshot": "hidden"},
                {"click": "newBannerReset"},
                {"snapshot": "shown"},
            ]
        )
        self.assertEqual(result["hidden"]["newBannerWhy"], "가린 필터: 요청 보내기 화면에서 보낸 요청 숨기기 1건.")
        shown = result["shown"]
        self.assertEqual(self.shown(shown), [6, 5, 3, 2, 1])
        self.assertTrue(shown["hideOptions"])
        self.assertTrue(shown["newBannerHidden"])

    def test_rows_of_requests_without_a_request_mode_and_of_delays(self) -> None:
        log = [
            exchange(1, httpMethod="OPTIONS", method=None, requestBody=None, status=204, responseBody=None, serverEra=None),
            exchange(2, httpMethod="GET", method=None, requestBody=None, status=405, responseBody={"error": "x"}, serverEra="modern"),
            exchange(3, injected=["server.delaySeconds=5"]),
            exchange(4, status=400, injected=["toolCall.delaySeconds=1"],
                     responseBody={"jsonrpc": "2.0", "id": 1, "error": {"code": -32021, "message": "m"}}),
        ]
        result = self.run_plan(
            [
                {"set": {"id": "fHideOptions", "checked": False}},
                {"snapshot": "list"},
                {"clickRow": 3},
                {"snapshot": "d3"},
                {"clickRow": 4},
                {"snapshot": "d4"},
                {"clickRow": 1},
                {"snapshot": "d1"},
                {"set": {"id": "fText", "value": "missingrequired", "event": "input"}},
                {"snapshot": "search"},
            ],
            log=log,
        )
        rows = {row["seq"]: row["cells"] for row in result["list"]["rows"]}
        self.assertEqual(rows[1][3:5], ["-", "-"])
        self.assertEqual(rows[2][3:5], ["-", "2026-07-28만"])
        self.assertEqual(rows[3][3:9], ["2025 방식", "둘 다", "200", "-", "정상", "server.delaySeconds=5"])
        self.assertEqual(rows[4][6:9], ["-32021 MissingRequiredClientCapability", "서버가 거절", "toolCall.delaySeconds=1"])
        self.assertIn(
            "정상 응답입니다. 늦게만 답했습니다: server.delaySeconds=5",
            result["d3"]["detail"],
        )
        self.assertIn("넣은 에러는 없고 늦게만 답했습니다: toolCall.delaySeconds=1", result["d4"]["detail"])
        self.assertNotIn("넣은 에러는 없습니다.", result["d4"]["detail"])
        self.assertIn("요청 방식: 알 수 없음", result["d1"]["detail"])
        self.assertEqual(self.shown(result["search"]), [4])


if __name__ == "__main__":
    unittest.main()
