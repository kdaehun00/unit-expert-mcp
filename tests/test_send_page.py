"""The /send screen: one /mcp request built from a form or from pasted text.

The parsing and the request building are pure functions of the shared screen
code (tests/test_ui_logic.py). These tests cover the page around them: the
route, the markup, and the script started on a stand-in DOM."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from mcp_test_support import HttpServerTestCase
from test_scenario_page import CONTROL_TAGS, NODE, parse
from unit_expert_mcp.server import (
    INSPECT_CONSOLE_SOURCE,
    INSPECT_SOURCE_HEADER,
    ROUTE_METHODS,
    SEND_TOOL_EXAMPLES,
    call_tool,
    encode_config_token,
    modern_test_tools,
    normalize_config,
    render_nav,
    render_send_page,
    tools,
)


class SendRouteTest(HttpServerTestCase):
    def get(self, path: str) -> tuple[int, dict[str, str], str]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read().decode("utf-8")
        finally:
            conn.close()

    def test_get_send_is_the_page(self) -> None:
        status, headers, body = self.get("/send?cfg=abc")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertEqual(body, render_send_page())
        self.assertEqual(ROUTE_METHODS["/send"], "GET, OPTIONS")

    def test_it_answers_like_the_other_screens(self) -> None:
        """Whatever /inspect answers to a method, /send answers the same."""
        for method in ("OPTIONS", "POST", "DELETE"):
            with self.subTest(method=method):
                answers = []
                for path in ("/inspect", "/send"):
                    conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
                    try:
                        conn.request(method, path)
                        response = conn.getresponse()
                        response.read()
                        answers.append((response.status, response.getheader("Allow")))
                    finally:
                        conn.close()
                self.assertEqual(answers[0], answers[1])


class SendPageStructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = render_send_page()
        cls.dom = parse(cls.page)

    def test_navigation_has_the_three_screens(self) -> None:
        self.assertIn(render_nav("send"), self.page)
        nav = next(element for element in self.dom.by_tag("nav"))
        links = [element for element in self.dom.by_tag("a") if element["attrs"].get("href") in {"/scenario", "/send", "/inspect"}]
        self.assertEqual([link["attrs"]["href"] for link in links[:3]], ["/scenario", "/send", "/inspect"])
        self.assertEqual([link["attrs"].get("aria-current") for link in links[:3]], [None, "page", None])
        for label in ("MCP 서버 만들기", "MCP 클라이언트", "호출 기록"):
            self.assertIn(label, nav["text"])

    def test_every_control_has_a_label_and_ids_are_unique(self) -> None:
        label_targets = {label["attrs"].get("for") for label in self.dom.by_tag("label")}
        controls = self.dom.by_tag(*CONTROL_TAGS)
        self.assertGreater(len(controls), 9)
        for control in controls:
            with self.subTest(control=control["attrs"].get("id")):
                self.assertTrue(control["in_label"] or control["attrs"].get("id") in label_targets)
        ids = [element["attrs"]["id"] for element in self.dom.elements if "id" in element["attrs"]]
        self.assertEqual(sorted({value for value in ids if ids.count(value) > 1}), [])
        for button in self.dom.by_tag("button"):
            self.assertEqual(button["attrs"].get("type"), "button")

    def test_paste_comes_first_then_the_form_then_the_response(self) -> None:
        order = [self.page.index(f'id="{name}"') for name in ("pasteBox", "cUrl", "cMode", "cMethod", "sendBtn", "lastResponse")]
        self.assertEqual(order, sorted(order))
        self.assertIn("요청 JSON, JSON-RPC 바디, curl 명령", self.dom.by_id("pasteBox")["attrs"]["placeholder"])
        self.assertEqual(
            [line.strip() for line in self.dom.by_id("cMode")["text"].splitlines() if line.strip()],
            ["2026 방식 (요청마다 버전을 실음)", "2025 방식 (initialize로 시작)"],
        )
        # Each copy button sits with the text it copies.
        keys = {button["attrs"]["data-copy"] for button in self.dom.by_tag("button") if "data-copy" in button["attrs"]}
        self.assertEqual(keys, {"requestFormat", "bundle", "curl"})
        for key in keys:
            self.assertEqual(self.dom.by_id("copy-" + key)["tag"], "pre")
        for element_id in ("pasteStatus", "copyStatus", "callNote"):
            self.assertEqual(self.dom.by_id(element_id)["attrs"].get("aria-live"), "polite")
        for element_id in ("pasteError", "argsError"):
            self.assertEqual(self.dom.by_id(element_id)["attrs"].get("role"), "alert")

    def test_no_external_reference(self) -> None:
        for needle in ('src="http', 'href="http', "@import", "//cdn", "onrender.com"):
            self.assertNotIn(needle, self.page)

    def test_the_example_arguments_work_on_the_real_tools(self) -> None:
        names = [tool["name"] for tool in tools()] + [tool["name"] for tool in modern_test_tools()]
        self.assertEqual(set(SEND_TOOL_EXAMPLES), set(names))
        for tool in tools():
            with self.subTest(tool=tool["name"]):
                result = call_tool({"name": tool["name"], "arguments": SEND_TOOL_EXAMPLES[tool["name"]]})
                self.assertFalse(result.get("isError"), result)
        for name in names:
            self.assertIn(f'<option value="{name}">', self.page)


SEND_STUB = r"""
const fs = require("fs");
const [scriptsPath, planPath] = process.argv.slice(2);
const scripts = JSON.parse(fs.readFileSync(scriptsPath, "utf8"));
const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
const elements = {};
const sent = [];
function fakeElement(id) {
  const node = {
    id: id, value: plan.values[id] === undefined ? "" : plan.values[id], textContent: "", hidden: plan.hiddenIds.indexOf(id) >= 0,
    disabled: false, checked: plan.checkedIds.indexOf(id) >= 0, href: "", open: false, className: "",
    dataset: {}, style: {}, listeners: {}, children: [], scrollHeight: 0, clientHeight: 0,
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener(type, handler) { (node.listeners[type] = node.listeners[type] || []).push(handler); },
    setAttribute() {}, append(...nodes) { node.children.push(...nodes); }, remove() {}, focus() {}, select() {},
    replaceChildren(...nodes) { node.children = nodes; },
    querySelector() { return null; }, querySelectorAll() { return []; }, closest() { return null; }
  };
  return node;
}
global.document = {
  getElementById(id) { return elements[id] || (elements[id] = fakeElement(id)); },
  querySelectorAll() { return []; },
  createElement(tag) { return fakeElement(tag); },
  addEventListener() {},
  body: fakeElement("body")
};
global.location = { origin: "http://localhost:8000", host: "localhost:8000", pathname: "/send", search: plan.search || "" };
global.navigator = {};
global.window = { addEventListener() {}, isSecureContext: false };
global.fetch = async (url, init) => {
  const body = (() => { try { return JSON.parse(init.body); } catch (error) { return init.body; } })();
  sent.push({ url: url, headers: init.headers, body: body });
  const answer = plan.answers[Math.min(sent.length - 1, plan.answers.length - 1)];
  return {
    status: answer.status, ok: answer.status >= 200 && answer.status < 300,
    text: async () => JSON.stringify(answer.body),
    headers: { get(name) { return (answer.headers || {})[name.toLowerCase()] || null; } }
  };
};
function el(id) { return document.getElementById(id); }
function fire(id, type) { (el(id).listeners[type] || []).forEach((handler) => handler({ key: "", preventDefault() {} })); }
function wait(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }
function snapshot() {
  const ids = ["cUrl", "cMode", "cMethod", "cTool", "cArgs", "hdrBox", "bodyBox", "pasteBox"];
  const out = { sent: sent.slice() };
  ids.forEach((id) => { out[id] = el(id).value; });
  ["pasteError", "pasteStatus", "argsError", "resultTitle", "resultText", "callNote", "lastResponse",
   "copy-bundle", "copy-curl", "copy-requestFormat", "metricRuns", "metricHits", "metricCache"].forEach((id) => { out[id] = el(id).textContent; });
  out.callFieldsHidden = el("callFields").hidden;
  out.initRowHidden = el("initRow").hidden;
  out.rawEditedHidden = el("rawEdited").hidden;
  out.rawOpen = el("rawBox").open;
  out.inspectLink = el("inspectLink").href;
  out.inspectLinkHidden = el("inspectLink").hidden;
  out.continueHidden = el("continueRow").hidden;
  out.chips = el("urlChips").children.map((chip) => chip.textContent);
  out.cache = el("clientCacheEnabled").checked;
  return out;
}
(async () => {
  for (const source of scripts) (0, eval)(source);
  const out = {};
  for (const step of plan.steps) {
    if (step.set) { el(step.set[0]).value = step.set[1]; fire(step.set[0], step.set[2] || "input"); }
    else if (step.check) { el(step.check[0]).checked = step.check[1]; }
    else if (step.click) { fire(step.click, "click"); await wait(step.wait || 30); }
    else if (step.snapshot) out[step.snapshot] = snapshot();
  }
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exit(1); });
"""

OK_ANSWER = {"status": 200, "body": {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}}


@unittest.skipUnless(NODE, "node is not installed")
class SendPageScriptTest(unittest.TestCase):
    """The page script on a stand-in DOM, with a stand-in fetch."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.workdir.name)
        (cls.root / "stub.js").write_text(SEND_STUB, encoding="utf-8")
        dom = parse(render_send_page())
        (cls.root / "scripts.json").write_text(json.dumps(dom.scripts), encoding="utf-8")
        cls.hidden_ids = [e["attrs"]["id"] for e in dom.elements if "hidden" in e["attrs"] and "id" in e["attrs"]]
        cls.checked_ids = [e["attrs"]["id"] for e in dom.elements if "checked" in e["attrs"] and "id" in e["attrs"]]
        cls.values = {
            e["attrs"]["id"]: e["attrs"]["value"]
            for e in dom.by_tag("input")
            if "value" in e["attrs"] and "id" in e["attrs"]
        }
        cls.values["cMode"] = "2026"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workdir.cleanup()

    def run_plan(
        self, steps: list[dict[str, Any]], search: str = "", answers: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        plan = {
            "steps": steps, "search": search, "answers": answers or [OK_ANSWER],
            "hiddenIds": self.hidden_ids, "checkedIds": self.checked_ids, "values": self.values,
        }
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        done = subprocess.run(
            [NODE, str(self.root / "stub.js"), str(self.root / "scripts.json"), str(path)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_it_starts_with_a_request_that_works_and_sends_nothing(self) -> None:
        first = self.run_plan([{"snapshot": "first"}])["first"]
        self.assertEqual((first["cUrl"], first["cMode"], first["cMethod"]), ("/mcp", "2026", "tools/list"))
        self.assertTrue(first["callFieldsHidden"])
        self.assertTrue(first["initRowHidden"])
        self.assertIn("Mcp-Method: tools/list", first["hdrBox"])
        self.assertEqual(json.loads(first["bodyBox"])["method"], "tools/list")
        self.assertEqual(first["sent"], [])
        self.assertTrue(first["rawEditedHidden"])
        # The three copy texts are what their buttons copy.
        self.assertEqual(json.loads(first["copy-bundle"]), {"url": "/mcp", "mode": "2026", "method": "tools/list"})
        self.assertTrue(first["copy-curl"].startswith("curl -i -sS -X POST 'http://localhost:8000/mcp'"))
        self.assertIn("MCP 테스트 요청 JSON 형식", first["copy-requestFormat"])

    def test_the_address_comes_from_the_link(self) -> None:
        token = encode_config_token(normalize_config({"toolCall": {"mode": "is-error"}}))
        first = self.run_plan([{"snapshot": "first"}], search=f"?cfg={token}")["first"]
        self.assertEqual(first["cUrl"], f"/mcp?cfg={token}")
        self.assertEqual(first["chips"], ["tools/call 응답 = is-error"])
        other = self.run_plan([{"snapshot": "first"}], search="?url=http%3A%2F%2Fh%2Fmcp")["first"]
        self.assertEqual(other["cUrl"], "http://h/mcp")

    def test_a_pasted_bundle_fills_the_form_and_is_sent(self) -> None:
        bundle = {
            "url": "/mcp?cfg=abc", "mode": "2026", "method": "tools/call", "tool": "convert_weight",
            "args": {"value": 2, "from_unit": "kg", "to_unit": "g"}, "omit": ["version-header"],
            "headers": {"Authorization": "Bearer t"},
        }
        result = self.run_plan(
            [
                {"set": ["pasteBox", json.dumps(bundle)]},
                {"click": "pasteApply"},
                {"snapshot": "filled"},
                {"click": "sendBtn"},
                {"snapshot": "sent"},
            ]
        )
        filled = result["filled"]
        self.assertEqual(filled["pasteError"], "")
        self.assertEqual(filled["pasteStatus"], "요청 JSON을 읽어 아래 칸을 채웠습니다.")
        self.assertEqual(
            (filled["cUrl"], filled["cMode"], filled["cMethod"], filled["cTool"]),
            ("/mcp?cfg=abc", "2026", "tools/call", "convert_weight"),
        )
        self.assertEqual(json.loads(filled["cArgs"]), bundle["args"])
        self.assertFalse(filled["callFieldsHidden"])
        self.assertNotIn("MCP-Protocol-Version", filled["hdrBox"])
        self.assertIn("Authorization: Bearer t", filled["hdrBox"])
        # What is copied as the current request is the bundle that was pasted.
        self.assertEqual(json.loads(filled["copy-bundle"]), bundle)
        sent = result["sent"]["sent"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["url"], "/mcp?cfg=abc")
        self.assertEqual(sent[0]["headers"]["Mcp-Name"], "convert_weight")
        self.assertEqual(sent[0]["headers"][INSPECT_SOURCE_HEADER], INSPECT_CONSOLE_SOURCE)
        self.assertEqual(sent[0]["body"]["params"]["arguments"], bundle["args"])
        self.assertEqual(result["sent"]["resultTitle"], "HTTP 200")
        self.assertEqual(result["sent"]["callNote"], "정상 응답입니다.")
        self.assertEqual(result["sent"]["inspectLink"], "/inspect?cfg=abc&screen=1")
        self.assertFalse(result["sent"]["inspectLinkHidden"])

    def test_a_pasted_json_rpc_body_and_a_curl_command(self) -> None:
        body = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "convert_length", "arguments": {"value": 9}}}
        curl = "curl -i -sS -X POST 'http://localhost:8000/mcp?cfg=zz' \\\n  -H 'Content-Type: application/json' \\\n  -H 'X-Only: 1' \\\n  -d '{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/list\",\"params\":{}}'"
        result = self.run_plan(
            [
                {"set": ["pasteBox", json.dumps(body)]},
                {"click": "pasteApply"},
                {"snapshot": "rpc"},
                {"set": ["pasteBox", curl]},
                {"click": "pasteApply"},
                {"snapshot": "curl"},
                {"click": "sendBtn"},
                {"snapshot": "sent"},
            ]
        )
        rpc = result["rpc"]
        self.assertEqual(rpc["pasteStatus"], "JSON-RPC 바디를 읽어 아래 칸을 채웠습니다.")
        self.assertEqual((rpc["cMode"], rpc["cMethod"], rpc["cTool"], rpc["cUrl"]), ("2025", "tools/call", "convert_length", "/mcp"))
        self.assertEqual(json.loads(rpc["cArgs"]), {"value": 9})
        self.assertEqual(json.loads(rpc["bodyBox"]), body)
        self.assertFalse(rpc["initRowHidden"])
        pasted = result["curl"]
        self.assertEqual(pasted["pasteStatus"], "curl 명령을 읽어 아래 칸을 채웠습니다.")
        self.assertEqual((pasted["cUrl"], pasted["cMode"], pasted["cMethod"]), ("http://localhost:8000/mcp?cfg=zz", "2025", "tools/list"))
        # The headers of the command are the whole list: nothing is added.
        self.assertEqual(pasted["hdrBox"], "Content-Type: application/json\nX-Only: 1")
        self.assertEqual(json.loads(pasted["bodyBox"])["id"], 3)
        self.assertTrue(pasted["rawOpen"])
        # 2025 mode: initialize first, then the request with the session it got.
        sent = result["sent"]["sent"]
        self.assertEqual([entry["body"]["method"] for entry in sent], ["initialize", "tools/list"])
        self.assertEqual(sent[1]["headers"]["X-Only"], "1")

    def test_a_legacy_request_carries_the_session_of_its_initialize(self) -> None:
        answers = [
            {"status": 200, "headers": {"mcp-session-id": "s-1"},
             "body": {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-06-18"}}},
            OK_ANSWER,
        ]
        result = self.run_plan(
            [{"set": ["cMode", "2025"]}, {"snapshot": "form"}, {"click": "sendBtn"}, {"snapshot": "sent"}],
            answers=answers,
        )
        self.assertFalse(result["form"]["initRowHidden"])
        self.assertNotIn("Mcp-Method", result["form"]["hdrBox"])
        first, second = result["sent"]["sent"]
        self.assertEqual(first["body"]["method"], "initialize")
        self.assertEqual(second["headers"]["Mcp-Session-Id"], "s-1")
        self.assertEqual(second["headers"]["MCP-Protocol-Version"], "2025-06-18")
        self.assertEqual(result["sent"]["resultText"], "initialize → HTTP 200 · tools/list → HTTP 200")
        # Without the checkbox the request goes alone.
        alone = self.run_plan(
            [{"set": ["cMode", "2025"]}, {"check": ["cInit", False]}, {"click": "sendBtn"}, {"snapshot": "sent"}]
        )["sent"]["sent"]
        self.assertEqual([entry["body"]["method"] for entry in alone], ["tools/list"])

    def test_bad_text_says_what_is_wrong_and_changes_nothing(self) -> None:
        result = self.run_plan(
            [
                {"snapshot": "before"},
                {"set": ["pasteBox", '{"method": "tools/list",}']},
                {"click": "pasteApply"},
                {"snapshot": "bad"},
                {"set": ["cMethod", "tools/call"]},
                {"set": ["cArgs", "{oops"]},
                {"snapshot": "args"},
            ]
        )
        bad = result["bad"]
        self.assertTrue(bad["pasteError"].startswith("JSON을 읽지 못했습니다: 1번째 줄 25번째 글자 근처"))
        self.assertEqual(bad["pasteStatus"], "")
        self.assertEqual(bad["bodyBox"], result["before"]["bodyBox"])
        self.assertTrue(result["args"]["argsError"].startswith("JSON을 읽지 못했습니다: 1번째 줄 2번째 글자 근처"))

    def test_an_error_answer_is_named(self) -> None:
        answers = [{"status": 200, "body": {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "boom"}}}]
        sent = self.run_plan([{"click": "sendBtn"}, {"snapshot": "sent"}], answers=answers)["sent"]
        self.assertEqual(sent["callNote"], "에러 응답입니다: -32603 boom")
        self.assertEqual(json.loads(sent["lastResponse"])["error"]["message"], "boom")
        self.assertEqual(sent["inspectLink"], "/inspect?plain=1&screen=1")

    def test_input_required_is_continued_round_by_round(self) -> None:
        from mcp_test_support import modern_call
        from unit_expert_mcp.server import handle_json_rpc

        def real(**params: Any) -> dict[str, Any]:
            payload, headers = modern_call(
                "convert_interactive", {"value": 3, "from_unit": "km"}, capabilities={"elicitation": {}}, **params
            )
            status, _, body = handle_json_rpc(payload, headers=headers)
            self.assertEqual(status, 200)
            return body

        first = real()
        second = real(
            inputResponses={"target_unit": {"action": "accept", "content": {"to_unit": "mm"}}},
            requestState=first["result"]["requestState"],
        )
        done = {"jsonrpc": "2.0", "id": 3, "result": {"resultType": "complete", "content": [], "isError": False}}
        result = self.run_plan(
            [
                {"snapshot": "start"},
                {"set": ["preset", "input-required", "change"]},
                {"click": "sendBtn"}, {"snapshot": "asked1"},
                {"click": "continueBtn"}, {"snapshot": "filled1"},
                {"click": "sendBtn"}, {"snapshot": "asked2"},
                {"click": "continueBtn"},
                {"click": "sendBtn"}, {"snapshot": "done"},
            ],
            answers=[{"status": 200, "body": body} for body in (first, second, done)],
        )
        self.assertTrue(result["start"]["continueHidden"])
        self.assertFalse(result["asked1"]["continueHidden"])
        # Filling the form sends nothing and takes the offer away until the next answer.
        filled = result["filled1"]
        self.assertEqual(len(filled["sent"]), 1)
        self.assertTrue(filled["continueHidden"])
        self.assertEqual(filled["pasteStatus"], "요청 JSON을 읽어 아래 칸을 채웠습니다.")
        self.assertEqual((filled["cMethod"], filled["cTool"]), ("tools/call", "convert_interactive"))
        self.assertEqual(json.loads(filled["cArgs"]), {"value": 3, "from_unit": "km"})
        self.assertFalse(result["asked2"]["continueHidden"])
        self.assertTrue(result["done"]["continueHidden"])

        one, two, three = (entry["body"] for entry in result["done"]["sent"])
        self.assertEqual(len({one["id"], two["id"], three["id"]}), 3)
        self.assertNotIn("requestState", one["params"])
        self.assertEqual(two["params"]["requestState"], first["result"]["requestState"])
        self.assertEqual(
            two["params"]["inputResponses"], {"target_unit": {"action": "accept", "content": {"to_unit": "mm"}}}
        )
        self.assertEqual(three["params"]["requestState"], second["result"]["requestState"])
        self.assertEqual(
            three["params"]["inputResponses"], {"precision": {"action": "accept", "content": {"decimals": 1}}}
        )
        for body in (one, two, three):
            self.assertEqual(body["params"]["arguments"], {"value": 3, "from_unit": "km"})
            self.assertEqual(body["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"], {"elicitation": {}})
        # The real server takes the requests the page made as they are.
        for entry, kind in zip(result["done"]["sent"], ("input_required", "input_required", "complete")):
            headers = {name: value for name, value in entry["headers"].items()}
            status, _, body = handle_json_rpc(entry["body"], headers=headers)
            self.assertEqual((status, body["result"]["resultType"]), (200, kind), body)
        self.assertEqual(body["result"]["content"][0]["text"], "3 km = 3000000.0 mm")

    def test_a_complete_or_legacy_answer_offers_no_continuation(self) -> None:
        sent = self.run_plan([{"click": "sendBtn"}, {"snapshot": "sent"}])["sent"]
        self.assertTrue(sent["continueHidden"])

    def test_the_cache_example_does_not_send_twice(self) -> None:
        answers = [{"status": 200, "body": {"jsonrpc": "2.0", "id": 1, "result": {"tools": [], "ttlMs": 60000}}}]
        result = self.run_plan(
            [{"set": ["preset", "tools-list-cache", "change"]}, {"snapshot": "example"}, {"click": "sendTwiceBtn", "wait": 60}, {"snapshot": "sent"}],
            answers=answers,
        )
        example = result["example"]
        self.assertTrue(example["cache"])
        token = encode_config_token(normalize_config({"toolsList": {"cacheTtlMs": 60000}}))
        self.assertEqual(example["cUrl"], f"/mcp?cfg={token}")
        sent = result["sent"]
        self.assertEqual(len(sent["sent"]), 1)
        self.assertEqual((sent["metricRuns"], sent["metricHits"], sent["metricCache"]), ("2", "1", "1"))
        self.assertEqual(sent["resultTitle"], "캐시 사용")


if __name__ == "__main__":
    unittest.main()
