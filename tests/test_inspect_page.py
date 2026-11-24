"""The /inspect page: structure (labels, ids, navigation, no external
reference), the literals other tests pin, and its script run under node on a
stand-in DOM against a stand-in /inspect/log."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from test_scenario_page import CONTROL_TAGS, NODE, parse
from unit_expert_mcp.server import (
    INSPECT_CONSOLE_SOURCE,
    INSPECT_SOURCE_HEADER,
    PREVIEW_SOURCE,
    encode_config_token,
    normalize_config,
    render_inspect_page,
    render_scenario_page,
    render_send_page,
)

MODERN_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"


class InspectPageStructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = render_inspect_page()
        cls.dom = parse(cls.page)

    def test_every_form_control_has_a_label(self) -> None:
        label_targets = {label["attrs"].get("for") for label in self.dom.by_tag("label")}
        controls = self.dom.by_tag(*CONTROL_TAGS)
        self.assertGreater(len(controls), 8)
        for control in controls:
            with self.subTest(control=control["attrs"]):
                self.assertTrue(
                    control["in_label"] or control["attrs"].get("id") in label_targets,
                    "control without a label",
                )

    def test_labels_point_at_existing_controls(self) -> None:
        control_ids = {control["attrs"].get("id") for control in self.dom.by_tag(*CONTROL_TAGS)}
        for label in self.dom.by_tag("label"):
            target = label["attrs"].get("for")
            if target is not None:
                with self.subTest(target=target):
                    self.assertIn(target, control_ids)

    def test_ids_are_unique(self) -> None:
        ids = [element["attrs"]["id"] for element in self.dom.elements if "id" in element["attrs"]]
        self.assertGreater(len(ids), 40)
        self.assertEqual(sorted({value for value in ids if ids.count(value) > 1}), [])

    def test_aria_references_exist(self) -> None:
        ids = {element["attrs"].get("id") for element in self.dom.elements}
        for element in self.dom.elements:
            for name in ("aria-describedby", "aria-labelledby"):
                for target in element["attrs"].get(name, "").split():
                    with self.subTest(attribute=name, target=target):
                        self.assertIn(target, ids)

    def test_no_external_reference(self) -> None:
        for needle in ('src="http', 'href="http', "src='http", "href='http", "@import", "//cdn", "onrender.com"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.page)
        for element in self.dom.by_tag("script", "link", "img", "iframe"):
            self.assertNotIn("src", element["attrs"])
            self.assertNotEqual(element["tag"], "link")

    def test_navigation_links(self) -> None:
        nav = next(element for element in self.dom.by_tag("nav"))
        links = {
            element["attrs"]["href"]: element
            for element in self.dom.by_tag("a")
            if element["attrs"].get("href") in {"/scenario", "/send", "/inspect"}
            and not element["attrs"].get("id") and not element["attrs"].get("class")
        }
        for label in ("MCP 서버 만들기", "MCP 클라이언트", "호출 기록"):
            self.assertIn(label, nav["text"])
        self.assertEqual(set(links), {"/scenario", "/send", "/inspect"})
        self.assertEqual(links["/inspect"]["attrs"].get("aria-current"), "page")

    def test_buttons_are_plain_buttons(self) -> None:
        buttons = self.dom.by_tag("button")
        self.assertGreater(len(buttons), 10)
        for button in buttons:
            with self.subTest(button=button["text"].strip()):
                self.assertEqual(button["attrs"].get("type"), "button")
                self.assertTrue(button["text"].strip())
        self.assertEqual(self.dom.by_tag("form"), [])

    def test_pinned_literals(self) -> None:
        for literal in (
            "x.injected",
            "x.status >= 200 && x.status < 400",
            'if (!exchange.status) return "연결 종료";',
        ):
            with self.subTest(literal=literal):
                self.assertIn(literal, self.page)
        self.assertNotIn("const ok = x.status < 400;", self.page)

    def test_layout_order(self) -> None:
        # Navigation, the address to look at, the folded filters, list, detail.
        positions = [
            self.page.index(needle)
            for needle in ('class="ue-top"', 'id="fCfg"', 'id="addrList"', 'id="filterBox"', 'id="rows"', 'id="detail"')
        ]
        self.assertEqual(positions, sorted(positions))
        # The address is the main input; every other filter is folded away.
        self.assertIn("MCP 주소를 붙여 넣으세요", self.dom.by_id("fCfg")["attrs"]["placeholder"])
        filters = self.dom.by_id("filterBox")
        self.assertEqual(filters["tag"], "details")
        self.assertNotIn("open", filters["attrs"])
        self.assertIn("자세히 거르기", filters["text"])
        for control_id in ("fText", "fKind", "fStatus", "fEra", "fMethod", "fHideScreen", "fHideOptions", "fErrorsOnly"):
            self.assertIn(f'id="{control_id}"', self.page[self.page.index('id="filterBox"'):self.page.index('id="hiddenBanner"')])
        # Sending a request has its own screen now.
        self.assertNotIn("consoleBox", self.page)
        self.assertNotIn("sendBtn", self.page)
        headers = [th["text"].strip() for th in self.dom.by_tag("th")]
        self.assertEqual(
            headers,
            ["시각", "HTTP", "메서드", "요청 방식", "서버", "상태", "에러 코드", "결과", "넣은 에러", "ms", "주소"],
        )

    def test_filter_controls_and_wording(self) -> None:
        for control_id in (
            "fText", "fKind", "fStatus", "fEra", "fMethod", "fCfg",
            "fHideScreen", "fHideOptions", "fErrorsOnly", "liveToggle",
        ):
            with self.subTest(control_id=control_id):
                self.dom.by_id(control_id)
        self.assertIn("지금까지 기록 숨기기", self.dom.by_id("hideBtn")["text"])
        self.assertIn("다시 보기", self.dom.by_id("unhideBtn")["text"])
        self.assertNotIn("화면 비우기", self.page)
        # One name per side: the request mode is "2025 방식" / "2026 방식".
        self.assertEqual(self.dom.by_id("fEra")["text"].split(), ["전체", "2025", "방식", "2026", "방식"])
        self.assertEqual(
            [line.strip() for line in self.dom.by_id("fKind")["text"].splitlines() if line.strip()],
            ["전체", "정상", "넣은 에러", "서버가 거절", "직접 지정한 응답", "연결 종료"],
        )
        # Words the screens no longer use.
        visible = self.page.split("<script>")[0]
        for old_name in ("레거시", "주입", "서버 세대", "cfg 토큰", "검증 거부", "RAW", "전역"):
            self.assertNotIn(old_name, visible)
        self.assertIn("MCP 서버 만들기에서 주소를 만들어 연결하면 여기에 나타납니다", self.page)

    def test_live_regions_and_focus_style(self) -> None:
        for element_id in ("resultCount", "liveStatus", "announce", "detailStatus"):
            with self.subTest(element_id=element_id):
                self.assertEqual(self.dom.by_id(element_id)["attrs"].get("aria-live"), "polite")
        for element_id in ("hiddenBanner", "newBanner"):
            self.assertEqual(self.dom.by_id(element_id)["attrs"].get("role"), "status")
        self.assertEqual(self.dom.by_id("cfgError")["attrs"].get("role"), "alert")
        self.assertIn(":focus-visible", self.page)

    def test_same_color_variables_as_the_scenario_page(self) -> None:
        def variables(page: str) -> dict[str, str]:
            root = page[page.index(":root {"):]
            root = root[: root.index("}")]
            pairs = (line.strip().rstrip(";").split(":", 1) for line in root.splitlines() if line.strip().startswith("--"))
            return {name.strip(): value.strip() for name, value in pairs}

        self.assertEqual(variables(self.page), variables(render_scenario_page()))

    def test_the_send_screen_names_itself_as_the_source(self) -> None:
        send = render_send_page()
        self.assertEqual(INSPECT_CONSOLE_SOURCE, "inspect-console")
        self.assertNotEqual(INSPECT_CONSOLE_SOURCE, PREVIEW_SOURCE)
        self.assertIn(json.dumps(INSPECT_SOURCE_HEADER), send)
        self.assertIn('"consoleSource": "inspect-console"', send)

    def test_the_record_screen_sends_nothing_to_mcp(self) -> None:
        script = parse(self.page).scripts[-1]
        # The only request is the one that reads the log.
        self.assertEqual(script.count("fetch("), 1)
        self.assertIn('fetch("/inspect/log" + query', script)
        self.assertIn('"&groups=1"', script)
        # The send screen has one fetch, in the function every send goes through.
        send = parse(render_send_page()).scripts[-1]
        self.assertEqual(send.count("fetch("), 1)
        self.assertIn("fetch(target", send[send.index("async function post("):send.index("async function sendRequest()")])


def exchange(seq: int, **fields: Any) -> dict[str, Any]:
    """One /inspect/log entry (a legacy tools/list answered normally)."""
    entry = {
        "seq": seq,
        "time": "03:04:05",
        "timestamp": f"2026-01-02T03:04:{seq % 60:02d}.678Z",
        "durationMs": 1.5,
        "httpMethod": "POST",
        "path": "/mcp",
        "query": "",
        "cfg": None,
        "configSource": "global",
        "source": None,
        "era": "2025",
        "method": "tools/list",
        "requestHeaders": {"Accept": "application/json", "Content-Type": "application/json"},
        "requestBody": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        "status": 200,
        "responseBody": {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
        "rawResponse": False,
        "responseHeaders": {},
        "serverEra": "both",
        "injected": [],
    }
    entry.update(fields)
    return entry


LEGACY_TOKEN = encode_config_token(normalize_config({"serverEra": "legacy"}))

REJECTED = exchange(
    3,
    era="2026",
    method="tools/call",
    status=400,
    cfg=LEGACY_TOKEN,
    query=f"cfg={LEGACY_TOKEN}",
    configSource="url",
    serverEra="legacy",
    requestHeaders={
        "Authorization": "Bearer abcdefghijklmnopqrstuvwxyz",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
        "Mcp-Name": "=?base64?7ZWc6riA?=",
    },
    requestBody={
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "convert_length", "arguments": {}, "_meta": {MODERN_VERSION_KEY: "2026-07-28"}},
    },
    responseBody={
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32020, "message": "Mcp-Method header does not match", "data": {"header": "tools/list"}},
    },
)


def base_log() -> list[dict[str, Any]]:
    """Oldest first."""
    return [
        exchange(1),
        exchange(2, source=PREVIEW_SOURCE),
        REJECTED,
        exchange(4, httpMethod="OPTIONS", method=None, requestBody=None, status=204, responseBody=None),
        exchange(
            5,
            method="tools/call",
            injected=["toolCall.mode=sse", "toolCall.delaySeconds=2"],
            requestBody={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "convert_length"}},
            responseBody={"events": [{"jsonrpc": "2.0", "method": "notifications/progress"}, {"jsonrpc": "2.0", "id": 2, "result": {}}]},
            durationMs=0.4,
        ),
    ]


@unittest.skipUnless(NODE, "node is not installed")
class InspectPageScriptTest(unittest.TestCase):
    """The page script on a stand-in DOM, polling a stand-in /inspect/log."""

    STUB = r"""
const fs = require("fs");
const [scriptsPath, planPath] = process.argv.slice(2);
const scripts = JSON.parse(fs.readFileSync(scriptsPath, "utf8"));
const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
let uid = 0;
const requests = [];
let serverLog = [];          // newest first, like the server
let failLog = false;

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.uid = ++uid;
    this.children = [];
    this.parent = null;
    this.attrs = {};
    this.listeners = {};
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.href = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    const self = this;
    this.classList = {
      list() { return self.className.split(/\s+/).filter(Boolean); },
      contains(name) { return this.list().indexOf(name) >= 0; },
      add(name) { if (!this.contains(name)) self.className = this.list().concat([name]).join(" "); },
      remove(name) { self.className = this.list().filter((item) => item !== name).join(" "); },
      toggle(name, force) {
        const on = force === undefined ? !this.contains(name) : Boolean(force);
        if (on) this.add(name); else this.remove(name);
        return on;
      }
    };
  }
  get hidden() { return "hidden" in this.attrs; }
  set hidden(value) { if (value) this.attrs.hidden = ""; else delete this.attrs.hidden; }
  get textContent() {
    return this.children.map((child) => (typeof child === "string" ? child : child.textContent)).join("");
  }
  set textContent(value) {
    this.children.forEach((child) => { if (typeof child !== "string") child.parent = null; });
    this.children = value === "" ? [] : [String(value)];
  }
  _adopt(node) {
    if (typeof node === "string") return node;
    if (!(node instanceof FakeElement)) throw new Error("append of a non-node: " + node);
    if (node.parent) node.remove();
    node.parent = this;
    return node;
  }
  append(...nodes) { nodes.forEach((node) => { this.children.push(this._adopt(node)); }); }
  prepend(...nodes) { this.children = nodes.map((node) => this._adopt(node)).concat(this.children); }
  replaceChildren(...nodes) { this.textContent = ""; this.append(...nodes); }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((child) => child !== this);
    this.parent = null;
  }
  setAttribute(name, value) {
    if (name === "href") this.href = String(value);
    this.attrs[name] = String(value);
  }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  hasAttribute(name) { return name in this.attrs; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  dispatch(type, event) {
    const full = Object.assign({ type: type, target: this, preventDefault() {}, stopPropagation() {} }, event || {});
    (this.listeners[type] || []).forEach((handler) => handler(full));
  }
  focus() { document.activeElement = this; this.dispatch("focus"); }
  select() {}
  scrollIntoView() {}
}

const elements = {};
global.document = {
  hidden: false,
  activeElement: null,
  listeners: {},
  getElementById(id) {
    if (!elements[id]) {
      elements[id] = new FakeElement("div");
      elements[id].id = id;
      elements[id].hidden = plan.hiddenIds.indexOf(id) >= 0;
    }
    return elements[id];
  },
  createElement(tag) { return new FakeElement(tag); },
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); },
  body: new FakeElement("body")
};
global.location = { origin: "http://localhost:8000", host: "localhost:8000", pathname: "/inspect", search: plan.search || "", hash: "" };
global.history = {
  replaceState(state, title, url) { const i = url.indexOf("?"); location.search = i < 0 ? "" : url.slice(i); }
};
global.navigator = {};
global.window = { addEventListener() {}, getSelection() { return ""; }, isSecureContext: false, innerHeight: 800 };
let tick = null;
global.setInterval = (handler, ms) => { tick = handler; global.intervalMs = ms; return 1; };

let groupRequests = 0;
global.fetch = async (url, init) => {
  // Every read of the log also asks for the addresses (groups=1).
  if (url.indexOf("&groups=1") >= 0) groupRequests += 1;
  requests.push(url.replace("&groups=1", ""));
  if (url.indexOf("/inspect/log") !== 0) throw new Error("unexpected request to " + url);
  if (failLog) return { ok: false, status: 503, json: async () => ({}) };
  const params = new URLSearchParams(url.split("?")[1] || "");
  let list = serverLog.slice();
  if (params.has("since")) list = list.filter((entry) => entry.seq > Number(params.get("since")));
  if (params.has("cfg")) list = list.filter((entry) => (entry.cfg || "") === params.get("cfg"));
  if (params.has("limit")) list = list.slice(0, Number(params.get("limit")));
  const body = { exchanges: list };
  // How the server says it recorded credentials (absent: an older answer).
  if (plan.secrets) body.secrets = plan.secrets;
  if (params.get("groups") === "1") {
    // The same summary as the server's inspect_groups().
    const groups = {};
    serverLog.slice().reverse().forEach((entry) => {
      if (entry.httpMethod === "OPTIONS") return;
      const key = entry.cfg || "";
      const group = groups[key] || (groups[key] = { cfg: entry.cfg || null, count: 0, screen: 0 });
      group.count += 1;
      if (entry.source) group.screen += 1;
      group.lastSeq = entry.seq;
      group.lastTimestamp = entry.timestamp;
    });
    body.groups = Object.keys(groups).map((key) => groups[key]).sort((a, b) => b.lastSeq - a.lastSeq);
  }
  return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)) };
};

function settle() { return new Promise((resolve) => setTimeout(resolve, 15)); }
function el(id) { return document.getElementById(id); }
function rowOf(seq) {
  return el("rows").children.filter((row) => row.attrs["data-seq"] === String(seq))[0];
}
function snapshot() {
  return {
    rows: el("rows").children.map((row) => ({
      uid: row.uid,
      seq: Number(row.attrs["data-seq"]),
      hidden: row.hidden,
      current: row.attrs["aria-current"] === "true",
      tabindex: row.attrs.tabindex,
      cells: row.children.map((cell) => cell.textContent),
      titles: row.children.map((cell) => cell.attrs.title || (cell.children[0] && cell.children[0].attrs ? cell.children[0].attrs.title || "" : ""))
    })),
    count: el("resultCount").textContent,
    caution: el("caution").textContent,
    emptyHidden: el("empty").hidden,
    emptyText: el("emptyText").textContent,
    newBannerHidden: el("newBanner").hidden,
    newBanner: el("newBannerText").textContent,
    hiddenBannerHidden: el("hiddenBanner").hidden,
    hiddenBanner: el("hiddenBannerText").textContent,
    liveStatus: el("liveStatus").textContent,
    detailHidden: el("detail").hidden,
    detailTitle: el("detailTitle").textContent,
    detail: el("detailBody").textContent,
    detailUid: el("detailBody").children.length ? el("detailBody").children[0].uid : null,
    detailStatus: el("detailStatus").textContent,
    workspace: el("workspace").className,
    addresses: el("addrList").children.map((item) => ({
      title: item.children[0].children[0].textContent,
      meta: item.children[0].children[1].textContent,
      pressed: item.children[0].attrs["aria-pressed"] === "true"
    })),
    addrBoxHidden: el("addrBox").hidden,
    scopeAll: el("scopeAll").attrs["aria-pressed"],
    scopePlain: el("scopePlain").attrs["aria-pressed"],
    cfgChips: el("cfgChips").children.map((chip) => chip.textContent),
    cfgSendLink: el("cfgSendLink").href,
    filterCount: el("filterCount").textContent,
    filterCountHidden: el("filterCount").hidden,
    filterBoxOpen: el("filterBox").open === true,
    groupRequests: groupRequests,
    cfgSummaryHidden: el("cfgSummary").hidden,
    cfgSummary: el("cfgSummaryText").textContent,
    cfgLink: el("cfgScenarioLink").href,
    cfgInput: el("fCfg").value,
    cfgError: el("cfgError").textContent,
    hideScreen: el("fHideScreen").checked,
    hideOptions: el("fHideOptions").checked,
    kind: el("fKind").value,
    live: el("liveToggle").checked,
    methods: el("fMethod").children.map((option) => option.attrs.value),
    search: location.search,
    intervalMs: global.intervalMs,
    host: el("ueHost").textContent,
    focused: document.activeElement ? Number(document.activeElement.attrs["data-seq"]) : null,
    requests: requests.slice()
  };
}
function findButton(node, text) {
  if (typeof node === "string") return null;
  if (node.tagName === "BUTTON" && node.textContent === text) return node;
  for (const child of node.children) {
    const found = findButton(child, text);
    if (found) return found;
  }
  return null;
}

(async () => {
  serverLog = plan.log.slice().reverse();
  for (const source of scripts) (0, eval)(source);
  await settle();
  const out = {};
  for (const step of plan.steps) {
    if (step.add) serverLog = step.add.slice().reverse().concat(serverLog);
    else if (step.replace) serverLog = step.replace.slice().reverse();
    else if (step.patch) Object.assign(serverLog.filter((entry) => entry.seq === step.patch.seq)[0], step.patch);
    else if (step.fail !== undefined) failLog = step.fail;
    else if (step.tick) { for (let i = 0; i < step.tick; i += 1) { tick(); await settle(); } }
    else if (step.documentHidden !== undefined) {
      document.hidden = step.documentHidden;
      (document.listeners.visibilitychange || []).forEach((handler) => handler());
    }
    else if (step.clickRow) rowOf(step.clickRow).dispatch("click");
    else if (step.clickCfg) rowOf(step.clickCfg).children[10].children[0].dispatch("click");
    else if (step.key) rowOf(step.key[0]).dispatch("keydown", { key: step.key[1] });
    else if (step.focusRow) rowOf(step.focusRow).focus();
    else if (step.click) el(step.click).dispatch("click");
    else if (step.clickAddress !== undefined) el("addrList").children[step.clickAddress].children[0].dispatch("click");
    else if (step.detailButton) findButton(el("detailBody"), step.detailButton).dispatch("click");
    else if (step.set) {
      const node = el(step.set.id);
      if ("value" in step.set) node.value = step.set.value;
      if ("checked" in step.set) node.checked = step.set.checked;
      node.dispatch(step.set.event || "change");
    }
    else if (step.snapshot) out[step.snapshot] = snapshot();
    await settle();
  }
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exit(1); });
"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.workdir.name)
        (cls.root / "stub.js").write_text(cls.STUB, encoding="utf-8")
        dom = parse(render_inspect_page())
        (cls.root / "scripts.json").write_text(json.dumps(dom.scripts), encoding="utf-8")
        # Elements the markup starts hidden.
        cls.hidden_ids = [
            element["attrs"]["id"] for element in dom.elements if "hidden" in element["attrs"]
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workdir.cleanup()

    def run_plan(
        self,
        steps: list[dict[str, Any]],
        log: list[dict[str, Any]] | None = None,
        search: str = "",
        secrets: str | None = None,
    ) -> dict[str, Any]:
        plan = {
            "search": search,
            "secrets": secrets,
            "log": base_log() if log is None else log,
            "steps": steps,
            "hiddenIds": self.hidden_ids,
        }
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

    @staticmethod
    def shown(snapshot: dict[str, Any]) -> list[int]:
        return [row["seq"] for row in snapshot["rows"] if not row["hidden"]]

    def test_inline_scripts_parse(self) -> None:
        scripts = parse(render_inspect_page()).scripts
        self.assertEqual(len(scripts), 3)
        for index, source in enumerate(scripts):
            with self.subTest(script=index):
                path = self.root / f"inspect-{index}.js"
                path.write_text(source, encoding="utf-8")
                done = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_start_without_any_record(self) -> None:
        first = self.run_plan([{"snapshot": "first"}], log=[])["first"]
        self.assertEqual(first["rows"], [])
        self.assertEqual(first["count"], "0건")
        self.assertFalse(first["emptyHidden"])
        self.assertIn("MCP 서버 만들기에서 주소를 만들어 연결하면 여기에 나타납니다", first["emptyText"])
        self.assertTrue(first["addrBoxHidden"])
        self.assertEqual((first["scopeAll"], first["scopePlain"]), ("true", "false"))
        self.assertEqual(first["groupRequests"], 1)
        self.assertTrue(first["detailHidden"])
        self.assertTrue(first["live"])
        self.assertEqual(first["intervalMs"], 2000)
        self.assertEqual(first["host"], "localhost:8000")
        # Loading the page asks for the log only. Nothing goes to /mcp.
        self.assertEqual(first["requests"], ["/inspect/log?since=0"])

    def test_first_load_shows_the_list_newest_first(self) -> None:
        first = self.run_plan([{"snapshot": "first"}])["first"]
        self.assertEqual([row["seq"] for row in first["rows"]], [5, 4, 3, 2, 1])
        # Requests sent by a screen and OPTIONS are hidden by default.
        self.assertEqual(self.shown(first), [5, 3, 1])
        self.assertEqual(first["count"], "5건 중 3건")
        self.assertTrue(first["emptyHidden"])
        self.assertTrue(first["newBannerHidden"])
        self.assertEqual(first["search"], "")
        rows = {row["seq"]: row for row in first["rows"]}
        rejected = rows[3]["cells"]
        self.assertEqual(
            rejected[1:8],
            ["POST", "tools/call convert_length", "2026 방식", "2025 방식만", "400", "-32020 HeaderMismatch", "서버가 거절"],
        )
        # The address column says what the address changes, not a piece of its code.
        self.assertEqual(rejected[10], "지원 스펙 = legacy")
        self.assertIn("지원 스펙 = legacy", rows[3]["titles"][10])
        self.assertEqual(rows[3]["titles"][4], "serverEra: legacy")
        self.assertEqual(rows[3]["titles"][0], "2026-01-02T03:04:03.678Z")
        injected = rows[5]["cells"]
        self.assertEqual(injected[7], "넣은 에러")
        self.assertEqual(injected[8], "toolCall.mode=sse 외 1")
        self.assertEqual(injected[9], "0.4")
        self.assertEqual(rows[1]["cells"][3:11], ["2025 방식", "둘 다", "200", "-", "정상", "", "1.5", "기본"])
        # The addresses that got requests, newest first. OPTIONS is not counted.
        self.assertEqual(
            [(address["title"], address["meta"].split(" · ")[0]) for address in first["addresses"]],
            [("기본 주소 (설정 없음)", "요청 3건 (MCP 클라이언트 화면 1건)"), ("지원 스펙 = legacy", "요청 1건")],
        )
        self.assertFalse(any(address["pressed"] for address in first["addresses"]))
        self.assertFalse(first["filterBoxOpen"])
        self.assertTrue(first["filterCountHidden"])
        # One row is the tab stop.
        self.assertEqual([row["seq"] for row in first["rows"] if row["tabindex"] == "0"], [5])
        self.assertIn("initialize", first["methods"])

    def test_polling_adds_rows_and_leaves_the_rest_alone(self) -> None:
        newer = [exchange(6, status=0, responseBody={"disconnect": True}, injected=["toolCall.mode=disconnect"]), exchange(7)]
        result = self.run_plan(
            [
                {"clickRow": 3},
                {"snapshot": "before"},
                {"add": newer},
                {"tick": 1},
                {"snapshot": "after"},
                {"tick": 2},
                {"snapshot": "idle"},
            ]
        )
        before, after, idle = result["before"], result["after"], result["idle"]
        self.assertEqual([row["seq"] for row in after["rows"]], [7, 6, 5, 4, 3, 2, 1])
        # The rows that were there are the same nodes in the same order.
        self.assertEqual([row["uid"] for row in after["rows"][2:]], [row["uid"] for row in before["rows"]])
        self.assertEqual(after["rows"][2:], before["rows"])
        # The open detail was not drawn again.
        self.assertFalse(after["detailHidden"])
        self.assertEqual(after["detailUid"], before["detailUid"])
        self.assertEqual(after["detailTitle"], "요청 상세 #3")
        self.assertTrue(next(row for row in after["rows"] if row["seq"] == 3)["current"])
        self.assertEqual(after["rows"][1]["cells"][5], "연결 종료")
        self.assertEqual(after["rows"][1]["cells"][7], "연결 종료")
        self.assertEqual(after["count"], "7건 중 5건")
        self.assertEqual([row["uid"] for row in idle["rows"]], [row["uid"] for row in after["rows"]])
        # First everything, then only what is newer. The poll after new rows
        # asks for the newest of them once more (durationMs), later polls ask
        # for nothing the page already has.
        self.assertEqual(
            idle["requests"],
            ["/inspect/log?since=0", "/inspect/log?since=0", "/inspect/log?since=5", "/inspect/log?since=7"],
        )

    def test_duration_is_corrected_on_the_next_poll(self) -> None:
        result = self.run_plan(
            [
                {"add": [exchange(6, durationMs=0.2)]},
                {"tick": 1},
                {"clickRow": 6},
                {"snapshot": "provisional"},
                {"patch": {"seq": 6, "durationMs": 2003.1}},
                {"tick": 1},
                {"snapshot": "final"},
            ]
        )
        self.assertEqual(result["provisional"]["rows"][0]["cells"][9], "0.2")
        self.assertEqual(result["final"]["rows"][0]["cells"][9], "2003.1")
        self.assertEqual(result["final"]["rows"][0]["uid"], result["provisional"]["rows"][0]["uid"])
        self.assertIn("2003.1 ms", result["final"]["detail"])
        self.assertEqual(result["final"]["detailUid"], result["provisional"]["detailUid"])

    def test_new_rows_hidden_by_the_filter_are_announced(self) -> None:
        result = self.run_plan(
            [
                {"add": [exchange(6, source="inspect-console"), exchange(7, source="scenario-page")]},
                {"tick": 1},
                {"snapshot": "hidden"},
                {"click": "newBannerReset"},
                {"snapshot": "reset"},
                {"set": {"id": "fHideScreen", "checked": False}},
                {"snapshot": "shown"},
            ]
        )
        self.assertEqual(self.shown(result["hidden"]), [5, 3, 1])
        self.assertFalse(result["hidden"]["newBannerHidden"])
        self.assertEqual(result["hidden"]["newBanner"], "새 요청 2건이 필터에 가려져 있습니다.")
        self.assertTrue(result["reset"]["newBannerHidden"])
        # Any request that names a source is a request sent by a screen.
        self.assertEqual(self.shown(result["shown"]), [7, 6, 5, 3, 2, 1])
        self.assertEqual(result["shown"]["search"], "?screen=1")

    def test_filters_hide_rows_and_go_to_the_url(self) -> None:
        result = self.run_plan(
            [
                {"set": {"id": "fKind", "value": "rejected"}},
                {"snapshot": "kind"},
                {"set": {"id": "fKind", "value": ""}},
                {"set": {"id": "fText", "value": "Bearer abcdef", "event": "input"}},
                {"snapshot": "text"},
                {"set": {"id": "fText", "value": "no such text", "event": "input"}},
                {"snapshot": "none"},
                {"click": "emptyReset"},
                {"set": {"id": "fHideOptions", "checked": False}},
                {"set": {"id": "fErrorsOnly", "checked": True}},
                {"snapshot": "errors"},
                {"click": "resetFilters"},
                {"snapshot": "reset"},
            ]
        )
        self.assertEqual(self.shown(result["kind"]), [3])
        self.assertEqual(result["kind"]["search"], "?kind=rejected")
        self.assertEqual(result["kind"]["count"], "5건 중 1건")
        self.assertEqual((result["kind"]["filterCount"], result["kind"]["filterCountHidden"]), ("바꿈 1", False))
        self.assertTrue(result["reset"]["filterCountHidden"])
        self.assertEqual(self.shown(result["text"]), [3])
        self.assertEqual(self.shown(result["none"]), [])
        self.assertFalse(result["none"]["emptyHidden"])
        self.assertIn("필터에 맞는 요청이 없습니다. 5건이 가려져 있습니다", result["none"]["emptyText"])
        self.assertEqual(self.shown(result["errors"]), [5, 3])
        self.assertEqual(result["errors"]["search"], "?options=1&errors=1")
        self.assertEqual(self.shown(result["reset"]), [5, 3, 1])
        self.assertEqual(result["reset"]["search"], "")
        # Filtering never asks the server again and keeps the row nodes.
        self.assertEqual(result["reset"]["requests"], ["/inspect/log?since=0"])
        self.assertEqual([row["uid"] for row in result["reset"]["rows"]], [row["uid"] for row in result["kind"]["rows"]])

    def test_start_from_a_cfg_link(self) -> None:
        query = f"?cfg={LEGACY_TOKEN}&kind=rejected"
        result = self.run_plan(
            [
                {"snapshot": "first"},
                {"add": [exchange(6, cfg=LEGACY_TOKEN, configSource="url", status=400, responseBody={"error": "x"}), exchange(7)]},
                {"tick": 1},
                {"snapshot": "polled"},
                {"click": "scopeAll"},
                {"snapshot": "cleared"},
            ],
            search=query,
        )
        first = result["first"]
        self.assertEqual([row["seq"] for row in first["rows"]], [3])
        # The field shows the whole address, as the config screen copies it.
        self.assertEqual(first["cfgInput"], f"http://localhost:8000/mcp?cfg={LEGACY_TOKEN}")
        self.assertEqual(first["kind"], "rejected")
        self.assertEqual(first["count"], "이 주소로 온 요청 1건")
        self.assertFalse(first["cfgSummaryHidden"])
        self.assertEqual(first["cfgSummary"], "이 주소에서 바꾼 설정")
        self.assertEqual(first["cfgChips"], ["지원 스펙 = legacy"])
        self.assertEqual(first["cfgLink"], f"/scenario#cfg={LEGACY_TOKEN}")
        self.assertEqual(first["cfgSendLink"], f"/send?cfg={LEGACY_TOKEN}")
        self.assertEqual((first["scopeAll"], first["scopePlain"]), ("false", "false"))
        # The addresses come from the whole log, whatever address is chosen.
        self.assertEqual([address["pressed"] for address in first["addresses"]], [False, True])
        # A link that carries a detailed filter opens with the filters in view.
        self.assertTrue(first["filterBoxOpen"])
        self.assertEqual(first["search"], query)
        self.assertEqual(first["requests"], [f"/inspect/log?since=0&cfg={LEGACY_TOKEN}"])
        # The server is asked for this cfg only.
        self.assertEqual([row["seq"] for row in result["polled"]["rows"]], [6, 3])
        self.assertEqual(result["polled"]["requests"][1], f"/inspect/log?since=2&cfg={LEGACY_TOKEN}")
        cleared = result["cleared"]
        self.assertEqual([row["seq"] for row in cleared["rows"]], [7, 6, 5, 4, 3, 2, 1])
        self.assertEqual(cleared["search"], "?kind=rejected")
        self.assertTrue(cleared["cfgSummaryHidden"])
        self.assertEqual(cleared["requests"][-1], "/inspect/log?since=0")
        self.assertEqual(cleared["cfgInput"], "")

    def test_the_address_list_and_the_two_scopes(self) -> None:
        result = self.run_plan(
            [
                {"clickAddress": 1},
                {"snapshot": "address"},
                {"click": "scopePlain"},
                {"snapshot": "plain"},
                {"clickAddress": 0},
                {"snapshot": "same"},
                {"click": "scopeAll"},
                {"snapshot": "all"},
            ]
        )
        address = result["address"]
        self.assertEqual([row["seq"] for row in address["rows"]], [3])
        self.assertEqual(address["search"], f"?cfg={LEGACY_TOKEN}")
        self.assertEqual([item["pressed"] for item in address["addresses"]], [False, True])
        self.assertEqual(address["cfgChips"], ["지원 스펙 = legacy"])
        # "기본 주소만": the requests whose address had no config. The server
        # is asked with an empty cfg=.
        plain = result["plain"]
        self.assertEqual([row["seq"] for row in plain["rows"]], [5, 4, 2, 1])
        self.assertEqual(plain["search"], "?plain=1")
        self.assertEqual(plain["requests"][-1], "/inspect/log?since=0&cfg=")
        self.assertEqual((plain["scopeAll"], plain["scopePlain"]), ("false", "true"))
        self.assertEqual(plain["count"], "기본 주소로 온 요청 4건 중 2건")
        self.assertEqual(plain["cfgInput"], "http://localhost:8000/mcp")
        self.assertTrue(plain["cfgSummaryHidden"])
        self.assertEqual([item["pressed"] for item in plain["addresses"]], [True, False])
        # Choosing what is already chosen reads nothing again.
        self.assertEqual(len(result["same"]["requests"]), len(plain["requests"]))
        everything = result["all"]
        self.assertEqual([row["seq"] for row in everything["rows"]], [5, 4, 3, 2, 1])
        self.assertEqual(everything["search"], "")
        self.assertEqual((everything["scopeAll"], everything["scopePlain"]), ("true", "false"))

    def test_cfg_filter_from_the_list_and_from_pasted_text(self) -> None:
        result = self.run_plan(
            [
                {"clickCfg": 3},
                {"snapshot": "clicked"},
                {"click": "scopeAll"},
                {"set": {"id": "fCfg", "value": f"http://localhost:8000/mcp?cfg={LEGACY_TOKEN}"}},
                {"click": "applyCfg"},
                {"snapshot": "pasted"},
                {"set": {"id": "fCfg", "value": "http://localhost:8000/mcp"}},
                {"click": "applyCfg"},
                {"snapshot": "plain"},
                {"set": {"id": "fCfg", "value": "두 낱말"}},
                {"click": "applyCfg"},
                {"snapshot": "bad"},
                {"set": {"id": "fCfg", "value": ""}},
                {"click": "applyCfg"},
                {"snapshot": "emptied"},
            ]
        )
        for name in ("clicked", "pasted"):
            with self.subTest(name=name):
                self.assertEqual([row["seq"] for row in result[name]["rows"]], [3])
                self.assertEqual(result[name]["search"], f"?cfg={LEGACY_TOKEN}")
                self.assertEqual(result[name]["cfgInput"], f"http://localhost:8000/mcp?cfg={LEGACY_TOKEN}")
        # Choosing the address in the list does not open the detail.
        self.assertTrue(result["clicked"]["detailHidden"])
        # An address without cfg= is the address without a config.
        self.assertEqual(result["plain"]["search"], "?plain=1")
        self.assertEqual(result["plain"]["cfgError"], "")
        # What is not an address says so and changes nothing.
        self.assertIn("MCP 주소로 읽지 못했습니다", result["bad"]["cfgError"])
        self.assertEqual(result["bad"]["search"], "?plain=1")
        # An emptied field shows every address again.
        self.assertEqual(result["emptied"]["search"], "")
        self.assertEqual(result["emptied"]["cfgError"], "")

    def test_an_unknown_cfg_shows_what_to_check(self) -> None:
        first = self.run_plan([{"snapshot": "first"}], search="?cfg=eyJ4IjoxfQ")["first"]
        self.assertEqual(first["rows"], [])
        self.assertIn("이 주소로 온 요청이 아직 없습니다", first["emptyText"])

    def test_detail_of_a_rejected_modern_request(self) -> None:
        result = self.run_plan(
            [
                {"clickRow": 3},
                {"snapshot": "masked"},
                {"detailButton": "값 보기"},
                {"snapshot": "revealed"},
                {"detailButton": "curl 명령 복사"},
                {"click": "closeDetail"},
                {"snapshot": "closed"},
            ],
            secrets="raw",  # the server recorded the header as received
        )
        masked = result["masked"]
        detail = masked["detail"]
        self.assertIn("has-detail", masked["workspace"])
        for text in (
            "서버가 거절",
            "요청이 규칙에 맞지 않아 서버가 거절했습니다. 넣은 에러는 없습니다.",
            "거절한 이유",
            "-32020 HeaderMismatch",
            "헤더와 바디가 다르거나 필수 헤더가 없음",
            "Mcp-Method header does not match",
            '"header": "tools/list"',
            "설정이 온 곳: 주소에 담긴 설정",
            "지원 스펙 = legacy",
            "MCP 서버 만들기에서 열기",
            "요청 방식: 2026 방식 · 서버가 지원하는 스펙: 2025 방식만 (legacy)",
            "헤더와 바디 비교",
            "디코드: 한글",
            f"POST /mcp?cfg={LEGACY_TOKEN}",
            "Bearer ***wxyz (len 26)",
            "HTTP 400",
            "curl로 다시 보내기",
            f"curl -i -sS -X POST 'http://localhost:8000/mcp?cfg={LEGACY_TOKEN}'",
            "-H 'Authorization: <Authorization 값>'",
            "Authorization 값은 가렸습니다.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, detail)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", detail)
        revealed = result["revealed"]["detail"]
        self.assertIn("Bearer abcdefghijklmnopqrstuvwxyz", revealed)
        self.assertIn("-H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz'", revealed)
        self.assertIn("값 가리기", revealed)
        # Showing the value redraws the header table only.
        self.assertEqual(result["revealed"]["detailUid"], masked["detailUid"])
        closed = result["closed"]
        self.assertTrue(closed["detailHidden"])
        self.assertNotIn("has-detail", closed["workspace"])
        self.assertEqual(closed["detail"], "")
        self.assertFalse(any(row["current"] for row in closed["rows"]))
        self.assertIn("요청 헤더가 그대로 남으니 실제 서비스 토큰은 보내지 마세요.", masked["caution"])

    def test_a_value_the_server_masked_is_shown_as_recorded(self) -> None:
        recorded = "Bearer ***wxyz (len 26, fp 1a2b3c4d)"
        entry = dict(REJECTED, requestHeaders=dict(REJECTED["requestHeaders"], Authorization=recorded))
        for secrets in ("masked", None):  # None: the page's own default
            with self.subTest(secrets=secrets):
                result = self.run_plan(
                    [{"clickRow": 3}, {"snapshot": "open"}], log=[exchange(1), entry], secrets=secrets
                )
                detail = result["open"]["detail"]
                for text in (
                    recorded,
                    "서버가 가려서 기록했습니다.",
                    "-H 'Authorization: <Authorization 값>'",
                    "Authorization 값은 서버가 가려서 기록했습니다. 실제 값으로 바꿔서 보내세요.",
                ):
                    self.assertIn(text, detail)
                # Nothing to reveal: the original is not in the log.
                self.assertNotIn("값 보기", detail)
                self.assertNotIn("'값 보기'를 누른 뒤", detail)
                # The notice follows what the log answer says; without it the markup's stays.
                self.assertEqual(
                    result["open"]["caution"],
                    "최근 200건을 모든 사용자가 함께 봅니다. 인증 헤더 값은 가려서 기록하고, 바디는 그대로 남습니다."
                    if secrets
                    else "",
                )

    def test_a_short_masked_value_still_gets_a_placeholder(self) -> None:
        entry = exchange(2, requestHeaders={"Authorization": "Bearer ***", "Accept": "application/json"})
        detail = self.run_plan([{"clickRow": 2}, {"snapshot": "open"}], log=[entry])["open"]["detail"]
        self.assertIn("-H 'Authorization: <Authorization 값>'", detail)
        self.assertIn("-H 'Accept: application/json'", detail)

    def test_detail_of_other_kinds(self) -> None:
        log = base_log() + [
            exchange(6, rawResponse=True, injected=["rawResponses"], responseHeaders={"X-Test": "1"}),
            exchange(7, status=0, responseBody={"disconnect": True}, injected=["toolCall.mode=disconnect"]),
            exchange(8, status=400, responseBody={"error": "invalid config token"}, configSource=None, serverEra=None),
            exchange(
                9,
                configSource="header",
                requestHeaders={"X-MCP-Test-Scenario": f"cfg:{LEGACY_TOKEN}"},
                serverEra="legacy",
            ),
        ]
        steps: list[dict[str, Any]] = []
        for seq in (5, 6, 7, 8, 9, 1):
            steps += [{"clickRow": seq}, {"snapshot": f"d{seq}"}]
        result = self.run_plan(steps, log=log)
        self.assertIn("설정으로 넣은 에러입니다: toolCall.mode=sse, toolCall.delaySeconds=2", result["d5"]["detail"])
        self.assertIn("SSE 응답: 이벤트 2개", result["d5"]["detail"])
        self.assertIn("이벤트 2 / 2", result["d5"]["detail"])
        self.assertNotIn("헤더와 바디 비교", result["d5"]["detail"])
        self.assertIn("설정에 직접 적어 둔 응답을 그대로 줬습니다.", result["d6"]["detail"])
        self.assertIn("X-Test", result["d6"]["detail"])
        self.assertIn("서버가 답하지 않고 연결을 끊었습니다. 넣은 에러: toolCall.mode=disconnect", result["d7"]["detail"])
        self.assertIn("응답 없음 (연결 종료)", result["d7"]["detail"])
        self.assertIn("거절한 이유", result["d8"]["detail"])
        self.assertIn("HTTP 400", result["d8"]["detail"])
        self.assertIn("invalid config token", result["d8"]["detail"])
        self.assertIn("설정이 온 곳: 알 수 없음", result["d8"]["detail"])
        self.assertIn("설정이 온 곳: X-MCP-Test-Scenario 헤더", result["d9"]["detail"])
        self.assertIn("지원 스펙 = legacy", result["d9"]["detail"])
        self.assertIn("정상 응답입니다.", result["d1"]["detail"])
        self.assertIn("설정이 온 곳: 서버 기본값", result["d1"]["detail"])
        self.assertNotIn("값 보기", result["d1"]["detail"])
        self.assertEqual(result["d1"]["detailTitle"], "요청 상세 #1")

    def test_keyboard_moves_through_the_visible_rows(self) -> None:
        result = self.run_plan(
            [
                {"focusRow": 5},
                {"key": [5, "ArrowDown"]},
                {"snapshot": "down"},
                {"key": [3, "Enter"]},
                {"snapshot": "open"},
                {"key": [3, "ArrowDown"]},
                {"snapshot": "next"},
                {"key": [1, "ArrowDown"]},
                {"key": [1, "Home"]},
                {"snapshot": "home"},
                {"key": [5, "Escape"]},
                {"snapshot": "escaped"},
            ]
        )
        # Row 4 (OPTIONS) is hidden, so the row after 5 is 3.
        self.assertEqual(result["down"]["focused"], 3)
        self.assertTrue(result["down"]["detailHidden"])
        self.assertEqual([row["seq"] for row in result["down"]["rows"] if row["tabindex"] == "0"], [3])
        self.assertEqual(result["open"]["detailTitle"], "요청 상세 #3")
        self.assertEqual(result["open"]["focused"], 3)
        # With the detail open, moving shows the next request.
        self.assertEqual(result["next"]["focused"], 1)
        self.assertEqual(result["next"]["detailTitle"], "요청 상세 #1")
        self.assertEqual(result["home"]["focused"], 5)
        self.assertTrue(result["escaped"]["detailHidden"])
        self.assertEqual(result["escaped"]["focused"], 5)

    def test_hiding_the_records_is_local_and_can_be_undone(self) -> None:
        result = self.run_plan(
            [
                {"click": "hideBtn"},
                {"snapshot": "hidden"},
                {"add": [exchange(6)]},
                {"tick": 1},
                {"snapshot": "newer"},
                {"click": "unhideBtn"},
                {"snapshot": "restored"},
            ]
        )
        hidden = result["hidden"]
        self.assertEqual(self.shown(hidden), [])
        self.assertFalse(hidden["hiddenBannerHidden"])
        self.assertIn("기록 5건을 이 화면에서만 숨겼습니다.", hidden["hiddenBanner"])
        self.assertIn("지금까지의 기록을 숨겼습니다.", hidden["emptyText"])
        self.assertEqual(self.shown(result["newer"]), [6])
        self.assertTrue(result["newer"]["newBannerHidden"])
        self.assertEqual(self.shown(result["restored"]), [6, 5, 3, 1])
        self.assertTrue(result["restored"]["hiddenBannerHidden"])
        # Nothing but reads of the log went to the server.
        self.assertTrue(all(url.startswith("/inspect/log?since=") for url in result["restored"]["requests"]))

    def test_polling_stops_while_off_or_hidden(self) -> None:
        result = self.run_plan(
            [
                {"set": {"id": "liveToggle", "checked": False}},
                {"add": [exchange(6)]},
                {"tick": 2},
                {"snapshot": "off"},
                {"click": "refreshBtn"},
                {"snapshot": "manual"},
                {"set": {"id": "liveToggle", "checked": True}},
                {"documentHidden": True},
                {"add": [exchange(7)]},
                {"tick": 2},
                {"snapshot": "tabHidden"},
                {"documentHidden": False},
                {"snapshot": "tabShown"},
            ]
        )
        self.assertEqual(len(result["off"]["rows"]), 5)
        self.assertEqual(result["off"]["requests"], ["/inspect/log?since=0"])
        self.assertEqual(len(result["manual"]["rows"]), 6)
        self.assertEqual(len(result["tabHidden"]["rows"]), 6)
        self.assertEqual(len(result["tabShown"]["rows"]), 7)

    def test_a_failed_poll_is_reported_and_recovers(self) -> None:
        result = self.run_plan(
            [
                {"fail": True},
                {"tick": 1},
                {"snapshot": "failed"},
                {"fail": False},
                {"tick": 1},
                {"snapshot": "recovered"},
            ]
        )
        self.assertEqual(result["failed"]["liveStatus"], "기록을 가져오지 못했습니다: HTTP 503")
        self.assertEqual(len(result["failed"]["rows"]), 5)
        self.assertEqual(result["recovered"]["liveStatus"], "")

    def test_a_restarted_server_is_read_again(self) -> None:
        restarted = [exchange(1, timestamp="2026-01-03T00:00:00.000Z", method="ping"), exchange(2, timestamp="2026-01-03T00:00:01.000Z")]
        result = self.run_plan(
            [
                {"clickRow": 3},
                {"replace": restarted},
                {"click": "refreshBtn"},
                {"snapshot": "after"},
            ]
        )
        after = result["after"]
        self.assertEqual([row["seq"] for row in after["rows"]], [2, 1])
        self.assertEqual(after["rows"][1]["cells"][2], "ping")
        self.assertTrue(after["detailHidden"])
        self.assertEqual(after["liveStatus"], "서버가 다시 시작되어 기록을 새로 읽었습니다.")


if __name__ == "__main__":
    unittest.main()
