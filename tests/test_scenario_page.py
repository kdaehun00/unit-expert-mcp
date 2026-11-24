"""Structure of the /scenario page: labels, ids, controls per option, navigation,
and that its inline scripts parse and start without an exception."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from unit_expert_mcp.server import (
    PUBLIC_MCP_URL,
    encode_config_token,
    normalize_config,
    render_inspect_page,
    render_nav,
    render_send_page,
    render_scenario_page,
    scenario_meta,
    set_active_config,
    set_active_scenario,
)

NODE = shutil.which("node")
VOID_TAGS = {"input", "meta", "br", "hr", "img", "link", "col", "source"}
CONTROL_TAGS = {"input", "select", "textarea"}


class PageParser(HTMLParser):
    """Collects what the checks below need from one page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, Any]] = []
        self.elements: list[dict[str, Any]] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = {
            "tag": tag,
            "attrs": {name: value or "" for name, value in attrs},
            "text": "",
            "in_label": any(parent["tag"] == "label" for parent in self.stack),
        }
        self.elements.append(element)
        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                closed = self.stack[index]
                del self.stack[index:]
                if tag == "script":
                    self.scripts.append(closed["text"])
                return

    def handle_data(self, data: str) -> None:
        for element in self.stack:
            element["text"] += data

    def by_tag(self, *tags: str) -> list[dict[str, Any]]:
        return [element for element in self.elements if element["tag"] in tags]

    def by_id(self, element_id: str) -> dict[str, Any]:
        return next(element for element in self.elements if element["attrs"].get("id") == element_id)


def parse(page: str) -> PageParser:
    parser = PageParser()
    parser.feed(page)
    parser.close()
    return parser


class ScenarioPageStructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = render_scenario_page()
        cls.dom = parse(cls.page)
        cls.meta = scenario_meta()

    def test_every_form_control_has_a_label(self) -> None:
        label_targets = {label["attrs"].get("for") for label in self.dom.by_tag("label")}
        controls = self.dom.by_tag(*CONTROL_TAGS)
        self.assertGreater(len(controls), 40)
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
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        self.assertEqual(duplicates, [])

    def test_aria_references_exist(self) -> None:
        ids = {element["attrs"].get("id") for element in self.dom.elements}
        for element in self.dom.elements:
            for name in ("aria-describedby", "aria-labelledby", "list"):
                for target in element["attrs"].get(name, "").split():
                    with self.subTest(attribute=name, target=target):
                        self.assertIn(target, ids)

    def test_no_external_reference(self) -> None:
        for needle in ('src="http', 'href="http', "src='http", "href='http", "@import", "//cdn"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.page)
        for element in self.dom.by_tag("script", "link", "img", "iframe"):
            self.assertNotIn("src", element["attrs"])
            self.assertNotEqual(element["tag"], "link")

    def test_every_option_has_a_control_and_a_visible_description(self) -> None:
        controls = {}
        for element in self.dom.elements:
            path = element["attrs"].get("data-config-path")
            if path:
                self.assertNotIn(path, controls, f"two controls for {path}")
                controls[path] = element
        # tests/test_server_era.py pins `<select id="serverEra">`, so that one
        # control is found by its id instead of data-config-path.
        controls["serverEra"] = self.dom.by_id("serverEra")
        rows = {
            element["attrs"]["data-opt"]: element
            for element in self.dom.elements
            if "data-opt" in element["attrs"]
        }
        self.assertEqual(set(controls), {option["path"] for option in self.meta["options"]})
        self.assertEqual(set(rows), set(controls))
        for option in self.meta["options"]:
            with self.subTest(path=option["path"]):
                row_text = rows[option["path"]]["text"]
                self.assertIn(option["label"], row_text)
                self.assertIn(option["path"], row_text)
                self.assertIn(option["description"], row_text)
                self.assertIn("기본값:", row_text)
                for line in option.get("help") or []:
                    self.assertIn(line, row_text)
                control = controls[option["path"]]
                if option["path"] != "serverEra":
                    described_by = control["attrs"]["aria-describedby"]
                    self.assertIn(option["description"], self.dom.by_id(described_by)["text"])
                for choice in option["choices"] or []:
                    self.assertIn(str(choice["value"]), row_text)
                    self.assertIn(str(choice["description"]), row_text)

    def rows_in(self, element_id: str) -> list[str]:
        """Option paths of the rows inside one part, in page order."""
        start = self.page.index(f'id="{element_id}"')
        ends = {"basicPart": 'id="advancedPart"', "advancedPart": 'id="errorPart"', "errorPart": 'id="copyPart"'}
        end = self.page.index(ends[element_id], start)
        return [
            element["attrs"]["data-opt"]
            for element in parse(self.page[start:end]).elements
            if "data-opt" in element["attrs"]
        ]

    def test_groups_follow_the_meta_order(self) -> None:
        groups = [element for element in self.dom.elements if "data-group" in element["attrs"]]
        # A group whose options are all basic ("기본 정보") has no card of its own.
        left = {option["group"] for option in self.meta["options"] if not option.get("basic")}
        expected = [group for group in self.meta["groups"] if group["id"] in left]
        self.assertEqual([group["id"] for group in self.meta["groups"] if group["id"] not in left], ["basic"])
        self.assertEqual([element["attrs"]["data-group"] for element in groups], [group["id"] for group in expected])
        for group, element in zip(expected, groups):
            with self.subTest(group=group["id"]):
                self.assertIn(group["label"], element["text"])
                # Every group, build or error, is a plain card: nothing to open.
                self.assertEqual(element["tag"], "section")

    def test_the_page_reads_address_then_basic_then_advanced_then_errors(self) -> None:
        """Top-down: the address to load a config from, the jumps, the four
        basic settings, the rest of the build options, the errors, the other
        copy forms, the 2026-07-28 compatibility matrix and the server-wide
        defaults."""
        names = ("loadCard", "toc", "basicPart", "advancedPart", "errorPart", "injectionCard", "copyPart",
                 "compatPart", "dangerZone")
        order = [self.page.index(f'id="{name}"') for name in names]
        self.assertEqual(order, sorted(order))
        # The address field comes before the two columns, right under the title.
        self.assertLess(self.page.index('id="loadCard"'), self.page.index('class="layout"'))
        self.assertLess(self.page.index("<h1>"), self.page.index('id="loadCard"'))
        basic, advanced, error = (self.rows_in(name) for name in ("basicPart", "advancedPart", "errorPart"))
        self.assertEqual(basic, ["mcp.identifier", "mcp.serviceName", "serverEra", "auth.mode"])
        self.assertEqual(basic, [option["path"] for option in self.meta["options"] if option.get("basic")])
        self.assertEqual(
            advanced,
            [o["path"] for o in self.meta["options"] if o["section"] == "build" and not o.get("basic")],
        )
        self.assertEqual(error, [o["path"] for o in self.meta["options"] if o["section"] == "error"])
        for path in ("strict2026", "allowedOrigins", "toolsList.pageSize", "session.validate", "auth.protectedTools"):
            self.assertIn(path, advanced)
        # The matrix is in none of the three.
        for name in ("basicPart", "advancedPart", "errorPart"):
            self.assertNotIn("matrix-cell", self.dom.by_id(name)["text"])
        self.assertIn("2026-07-28 호환 테스트", self.dom.by_id("compatPart")["text"])

    def test_every_setting_is_in_view_without_a_click(self) -> None:
        # The only thing that folds is the server-wide (dangerous) box.
        self.assertEqual([e["attrs"].get("id") for e in self.dom.by_tag("details")], ["dangerZone"])
        self.assertEqual(self.dom.by_tag("summary")[0]["text"].split()[0], "서버")
        visible = self.dom.by_tag("main")[0]["text"]
        for word in ("전체 보기", "자세히", "세부 설정", "더 보기", "펼치"):
            with self.subTest(word=word):
                self.assertNotIn(word, visible)
        self.assertEqual([e for e in self.dom.elements if "data-expand" in e["attrs"]], [])
        self.assertNotIn("syncExpandButtons", self.page)
        for name, title, count_id, count in (
            ("advancedPart", "자세한 설정", "advancedCount", "기본값과 다른 설정 0개"),
            ("errorPart", "에러 넣기", "errorCount", "켜진 것 0개"),
        ):
            with self.subTest(name=name):
                part = self.dom.by_id(name)
                self.assertEqual(part["tag"], "section")
                self.assertIn(title, part["text"])
                self.assertEqual(self.dom.by_id(count_id)["text"], count)
        # No row is rendered hidden.
        for element in self.dom.elements:
            if "data-opt" in element["attrs"] or "data-group" in element["attrs"]:
                self.assertNotIn("hidden", element["attrs"])

    def test_the_jumps_name_every_part(self) -> None:
        nav = self.dom.by_id("toc")
        self.assertEqual(nav["tag"], "nav")
        jumps = [e for e in self.dom.by_tag("button") if "data-jump" in e["attrs"]]
        self.assertEqual(
            [(e["attrs"]["data-jump"], e["text"]) for e in jumps],
            [("basicPart", "기본 설정"), ("advancedPart", "자세한 설정"), ("errorPart", "에러 넣기"),
             ("copyPart", "다른 형태로 복사"), ("compatPart", "호환 테스트"), ("dangerZone", "서버 기본값 바꾸기")],
        )
        for element in jumps:
            self.dom.by_id(element["attrs"]["data-jump"])
        # Buttons, not #links: the address hash carries #cfg.
        self.assertNotIn('href="#', self.page)

    def test_the_address_to_load_from_is_one_field_at_the_top(self) -> None:
        card = self.dom.by_id("loadCard")
        self.assertEqual(card["tag"], "section")
        for text in ("주소로 불러오기", "가지고 있는 MCP 주소를 넣으면 그 주소의 설정이 아래에 채워집니다", "불러오기"):
            self.assertIn(text, card["text"])
        field = self.dom.by_id("loadToken")
        self.assertEqual((field["tag"], field["attrs"]["type"]), ("input", "text"))
        self.assertNotIn("readonly", field["attrs"])
        self.assertEqual(self.dom.by_tag("textarea")[0]["attrs"]["data-config-path"], "rawResponses")
        # It is not the address the page makes: the two are named apart.
        self.assertEqual(self.dom.by_id("urlTitle")["text"], "만들어진 주소")
        self.assertNotIn("내 MCP 서버 주소", self.page)

    def test_choices_of_the_basic_selects_are_explained_one_line_each(self) -> None:
        rows = {e["attrs"]["data-opt"]: e for e in self.dom.elements if "data-opt" in e["attrs"]}
        for path, names in (
            ("serverEra", ("둘 다", "2025 방식만", "2026-07-28만")),
            ("auth.mode", ("끔", "모든 요청에 토큰", "일부 툴만 토큰")),
        ):
            option = next(o for o in self.meta["options"] if o["path"] == path)
            self.assertEqual(tuple(choice["label"] for choice in option["choices"]), names)
            for choice in option["choices"]:
                with self.subTest(path=path, value=choice["value"]):
                    self.assertIn(choice["label"], rows[path]["text"])
                    self.assertIn(choice["description"], rows[path]["text"])
        # The select shows the plain names, not the config values.
        era_options = [e["text"] for e in self.dom.by_tag("option") if e["attrs"].get("value") in {"both", "legacy", "modern"}]
        self.assertEqual(era_options[:3], ["둘 다", "2025 방식만", "2026-07-28만"])
        self.assertIn("잘 모르겠으면", rows["serverEra"]["text"])

    def test_strict_and_origin_say_what_they_check(self) -> None:
        rows = {e["attrs"]["data-opt"]: e for e in self.dom.elements if "data-opt" in e["attrs"]}
        strict = rows["strict2026"]["text"]
        for text in ("필수 헤더", "헤더와 바디 일치", "_meta 필수 값", "HTTP 400", "-32020", "-32602",
                     "-32022", "끄면 통과되는 것", "꺼도 검사하는 것"):
            with self.subTest(option="strict2026", text=text):
                self.assertIn(text, strict)
        origin = rows["allowedOrigins"]["text"]
        for text in ("브라우저가 요청에 붙이는 출발지 주소", "HTTP 403", "비워 두면 검사하지 않습니다",
                     "Origin 헤더가 없는 요청(curl", "https://app.example.com"):
            with self.subTest(option="allowedOrigins", text=text):
                self.assertIn(text, origin)
        # The lines are a plain list under the control, in view without a click.
        helps = [e for e in self.dom.by_tag("ul") if e["attrs"].get("class") == "opt-help"]
        self.assertEqual(len(helps), len([o for o in self.meta["options"] if o.get("help")]))

    def test_every_preset_has_a_button(self) -> None:
        buttons = {
            element["attrs"]["data-preset"]: element
            for element in self.dom.by_tag("button")
            if "data-preset" in element["attrs"]
        }
        preset_buttons = [e for e in self.dom.by_tag("button") if "data-preset" in e["attrs"]]
        # The matrix cells and the error presets have a button. A preset that
        # only sets build options has none: its options are in view at the top.
        shown = [
            preset for preset in self.meta["presets"]
            if preset["category"] == "matrix" or preset["section"] == "error"
        ]
        self.assertEqual(len(preset_buttons), len(shown))
        self.assertEqual(set(buttons), {preset["id"] for preset in shown})
        error_buttons = self.dom.by_id("injectionCard")["text"]
        for preset in self.meta["presets"]:
            if preset["category"] == "injection":
                self.assertEqual(preset["label"] in error_buttons, preset["section"] == "error", preset["id"])
        self.assertIn("지원하지 않는 조합", buttons["matrix-legacy-modern"]["text"])
        for preset_id in ("matrix-modern-modern", "matrix-modern-legacy", "matrix-legacy-legacy"):
            self.assertNotIn("지원하지 않는", buttons[preset_id]["text"])

    def test_buttons_are_plain_buttons(self) -> None:
        buttons = self.dom.by_tag("button")
        self.assertGreater(len(buttons), 20)
        for button in buttons:
            with self.subTest(button=button["text"].strip()):
                self.assertEqual(button["attrs"].get("type"), "button")
                self.assertTrue(button["text"].strip() or button["attrs"].get("aria-label"))
        self.assertEqual(self.dom.by_tag("form"), [])

    def test_navigation_links(self) -> None:
        nav = next(element for element in self.dom.by_tag("nav"))
        links = {
            element["attrs"]["href"]: element
            for element in self.dom.by_tag("a")
            if element["attrs"].get("href") in {"/scenario", "/send", "/inspect"}
        }
        for label in ("MCP 서버 만들기", "MCP 클라이언트", "호출 기록"):
            self.assertIn(label, nav["text"])
        for old_name in ("서버 설정", "요청 보내기", "요청 기록"):
            self.assertNotIn(old_name, nav["text"])
        self.assertEqual(links["/scenario"]["attrs"].get("aria-current"), "page")
        self.assertNotIn("aria-current", links["/inspect"]["attrs"])
        self.assertNotIn("aria-current", links["/send"]["attrs"])

    def test_each_screen_is_titled_with_its_tab_name(self) -> None:
        for name, page in (
            ("MCP 서버 만들기", self.page), ("MCP 클라이언트", render_send_page()), ("호출 기록", render_inspect_page()),
        ):
            with self.subTest(name=name):
                dom = parse(page)
                self.assertEqual(dom.by_tag("title")[0]["text"], f"Unit Expert MCP · {name}")
                self.assertEqual(dom.by_tag("h1")[0]["text"], name)
                visible = page.split("<script>")[0]
                for old_name in ("서버 설정", "요청 보내기", "요청 기록"):
                    self.assertNotIn(old_name, visible)

    def test_inspect_page_has_the_same_navigation(self) -> None:
        page = render_inspect_page()
        self.assertIn(render_nav("inspect"), page)
        self.assertIn(render_nav("scenario"), self.page)
        self.assertIn(render_nav("send"), render_send_page())
        self.assertEqual(
            render_nav("inspect").replace(' aria-current="page"', ""),
            render_nav("scenario").replace(' aria-current="page"', ""),
        )
        ids = [element["attrs"]["id"] for element in parse(page).elements if "id" in element["attrs"]]
        self.assertEqual(sorted({value for value in ids if ids.count(value) > 1}), [])

    def test_live_regions(self) -> None:
        for element_id in ("copyStatus", "loadStatus", "dangerStatus", "searchCount"):
            with self.subTest(element_id=element_id):
                self.assertEqual(self.dom.by_id(element_id)["attrs"].get("aria-live"), "polite")
        for element_id in ("urlError", "loadError"):
            self.assertEqual(self.dom.by_id(element_id)["attrs"].get("role"), "alert")
        self.assertIn(":focus-visible", self.page)

    def test_removed_controls_are_gone(self) -> None:
        for needle in ("data-era=", "era-button", "era-toggle", "rejectLegacyEnabled", "applyConfig"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.page)
        self.assertNotIn('data-config-path="rejectLegacy"', self.page)
        self.assertNotIn('data-config-path="protocolEra"', self.page)
        button_texts = [button["text"].strip() for button in self.dom.by_tag("button")]
        self.assertNotIn("적용", button_texts)
        self.assertNotIn(PUBLIC_MCP_URL, self.page)
        self.assertNotIn("onrender.com", self.page)
        self.assertNotIn("시나리오별 설명", self.page)

    def test_danger_zone_is_collapsed_and_last(self) -> None:
        column = self.dom.by_tag("details")
        self.assertEqual(column[-1]["attrs"].get("id"), "dangerZone")
        self.assertNotIn("open", self.dom.by_id("dangerZone")["attrs"])
        self.assertLess(self.page.index('id="compatPart"'), self.page.index('id="dangerZone"'))
        danger = column[-1]["text"]
        for text in ("지금 설정을 서버 기본값으로", "서버 기본값 되돌리기", "세션 모두 끊기", "모든 사용자에게 적용"):
            self.assertIn(text, danger)

    def test_the_address_panel_says_what_it_is_and_what_each_button_copies(self) -> None:
        card = self.dom.by_id("resultCard")["text"]
        for text in ("만들어진 주소", "이 주소를 MCP 클라이언트에 넣으면 지금 설정대로 동작합니다.", "주소 복사",
                     "기본값에서 바꾼 설정", "모두 되돌리기", "이 주소의 호출 기록 보기", "MCP 클라이언트로 요청 보내 보기"):
            self.assertIn(text, card)
        # The address is the one filled button of the card; the links are secondary.
        self.assertEqual(self.dom.by_id("copyUrl")["attrs"].get("class"), None)
        for link in ("sendLink", "inspectLink"):
            self.assertEqual(self.dom.by_id(link)["attrs"]["class"], "button secondary")
        # The card holds the address only. Everything else that can be copied
        # is in "다른 형태로 복사", in view; each button sits with the whole text it copies.
        self.assertEqual(len([e for e in self.dom.by_tag("section", "details") if e["attrs"].get("id") in
                              {"resultCard", "copyBox"}]), 1)
        self.assertEqual(self.dom.by_id("copyBox")["tag"], "div")
        copy_buttons = [e for e in self.dom.by_tag("button") if "data-copy" in e["attrs"]]
        for button in copy_buttons:
            key = button["attrs"]["data-copy"]
            if key == "url":
                continue
            with self.subTest(key=key):
                self.assertEqual(self.dom.by_id("copy-" + key)["tag"], "pre")
                expand = [e for e in self.dom.by_tag("button") if e["attrs"].get("data-expand") == "copy-" + key]
                self.assertEqual(expand, [])
        self.assertEqual(
            {button["attrs"]["data-copy"] for button in copy_buttons},
            {"url", "curlModern", "curlLegacy", "token", "header", "configJson", "cliProbe", "cliRun",
             "configFormat", "matrixCli"},
        )
        self.assertNotIn("copyMoreToggle", self.page)
        self.assertNotIn("text-overflow: ellipsis", self.page.split("</style>")[0].split(".addr-title")[0])

    def test_the_form_does_not_start_from_the_global_config(self) -> None:
        try:
            set_active_config({"toolCall": {"mode": "sse"}, "mcp": {"serviceName": "전역 <b>이름</b>"}})
            page = render_scenario_page()
        finally:
            set_active_scenario("ok")
        dom = parse(page)
        # No control is rendered with a value; the script fills the form from
        # the defaults. The global config only travels as data for the banner.
        for control in dom.by_tag(*CONTROL_TAGS):
            self.assertNotIn("checked", control["attrs"])
            if control["attrs"].get("type") != "checkbox":
                self.assertNotIn("value", control["attrs"])
        self.assertFalse(any("selected" in option["attrs"] for option in dom.by_tag("option")))
        self.assertIn("globalBanner", page)
        self.assertNotIn("<b>이름</b>", page)
        self.assertIn("\\u003cb>이름\\u003c/b>", page)


@unittest.skipUnless(NODE, "node is not installed")
class ScenarioPageScriptTest(unittest.TestCase):
    """The inline scripts, checked and started under node with a stand-in DOM."""

    STUB = r"""
const fs = require("fs");
const [scriptsPath, hash, decodePath, paste] = process.argv.slice(2);
const scripts = JSON.parse(fs.readFileSync(scriptsPath, "utf8"));
const decodeBody = decodePath ? JSON.parse(fs.readFileSync(decodePath, "utf8")) : null;
const elements = {};
const requests = [];
function fakeElement(id) {
  return {
    id: id, value: "", textContent: "", hidden: false, disabled: false, checked: false, href: "",
    dataset: {}, style: {}, open: false, listeners: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    setAttribute() {}, append() {}, remove() {}, focus() {}, select() {},
    querySelector() { return null; }, querySelectorAll() { return []; }, closest() { return null; }
  };
}
global.document = {
  getElementById(id) { return elements[id] || (elements[id] = fakeElement(id)); },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  createElement(tag) { return fakeElement(tag); },
  addEventListener() {},
  body: fakeElement("body")
};
global.location = { origin: "http://localhost:8000", host: "localhost:8000", pathname: "/scenario", search: "", hash: hash || "" };
global.history = { replaceState(state, title, url) { const i = url.indexOf("#"); location.hash = i < 0 ? "" : url.slice(i); } };
global.navigator = {};
global.window = { addEventListener() {}, confirm() { return true; }, isSecureContext: false };
global.fetch = async (url, init) => {
  const payload = JSON.parse(init.body);
  requests.push({ url: url, action: payload.action || null });
  const body = decodeBody || { ok: true, config: payload.config, compact: {}, token: null };
  return { status: 200, json: async () => body };
};
for (const source of scripts) (0, eval)(source);
if (paste) {
  document.getElementById("loadToken").value = paste;
  document.getElementById("loadTokenButton").listeners.click();
}
setTimeout(() => {
  const el = (id) => document.getElementById(id);
  process.stdout.write(JSON.stringify({
    advancedCount: el("advancedCount").textContent,
    errorCount: el("errorCount").textContent,
    loadStatus: el("loadStatus").textContent,
    url: el("mcpUrl").value,
    inspect: el("inspectLink").href,
    host: el("ueHost").textContent,
    urlError: el("urlError").textContent,
    loadError: el("loadError").textContent,
    token: el("copy-token").textContent,
    header: el("copy-header").textContent,
    curl: el("copy-curlLegacy").textContent,
    cli: el("copy-cliRun").textContent,
    chipsEmptyHidden: el("chipsEmpty").hidden,
    bannerHidden: el("globalBanner").hidden,
    banner: el("globalBannerText").textContent,
    hash: location.hash,
    requests: requests
  }));
}, 50);
"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.workdir.name)
        (cls.root / "stub.js").write_text(cls.STUB, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workdir.cleanup()

    def start(
        self,
        page: str,
        url_hash: str = "",
        decode_body: dict[str, Any] | None = None,
        paste: str = "",
    ) -> dict[str, Any]:
        """decode_body: what the stand-in server answers to decode and validate.
        paste: text put into "주소로 불러오기" and loaded once the page has started."""
        scripts = parse(page).scripts
        (self.root / "scripts.json").write_text(json.dumps(scripts), encoding="utf-8")
        args = [NODE, str(self.root / "stub.js"), str(self.root / "scripts.json"), url_hash]
        if decode_body is not None:
            (self.root / "decode.json").write_text(json.dumps(decode_body), encoding="utf-8")
            args.append(str(self.root / "decode.json"))
        elif paste:
            args.append("")
        if paste:
            args.append(paste)
        done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_inline_scripts_parse(self) -> None:
        for name, page in (
            ("scenario", render_scenario_page()), ("inspect", render_inspect_page()), ("send", render_send_page())
        ):
            scripts = parse(page).scripts
            self.assertGreaterEqual(len(scripts), 3)
            for index, source in enumerate(scripts):
                with self.subTest(page=name, script=index):
                    path = self.root / f"{name}-{index}.js"
                    path.write_text(source, encoding="utf-8")
                    done = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
                    self.assertEqual(done.returncode, 0, done.stderr)

    def test_start_with_the_defaults(self) -> None:
        result = self.start(render_scenario_page())
        self.assertEqual(result["url"], "http://localhost:8000/mcp")
        self.assertEqual(result["inspect"], "/inspect")
        self.assertEqual(result["host"], "localhost:8000")
        self.assertEqual(result["urlError"], "")
        # With nothing changed there is no config code: the item says so instead of being blank.
        self.assertEqual(result["token"], "바꾼 설정이 없어 설정 코드가 없습니다. 기본 주소만 쓰면 됩니다.")
        self.assertTrue(result["curl"].startswith("curl -i -sS -X POST 'http://localhost:8000/mcp'"))
        self.assertIn("run all --expect server --url 'http://localhost:8000/mcp'", result["cli"])
        self.assertFalse(result["chipsEmptyHidden"])
        self.assertTrue(result["bannerHidden"])
        self.assertEqual(result["hash"], "")
        # Nothing is sent on load: no preview request, no validation, no global change.
        self.assertEqual(result["requests"], [])
        self.assertEqual((result["advancedCount"], result["errorCount"]), ("기본값과 다른 설정 0개", "켜진 것 0개"))
        self.assertEqual((result["loadStatus"], result["loadError"]), ("", ""))

    def start_from(self, patch: dict[str, Any]) -> dict[str, Any]:
        config = normalize_config(patch)
        token = encode_config_token(config)
        return self.start(render_scenario_page(), f"#cfg={token}", {"ok": True, "config": config, "token": token})

    def test_a_cfg_address_shows_how_many_settings_each_part_holds(self) -> None:
        advanced = self.start_from({"strict2026": False, "allowedOrigins": ["https://app.example.com"]})
        self.assertEqual((advanced["advancedCount"], advanced["errorCount"]), ("기본값과 다른 설정 2개", "켜진 것 0개"))

        error = self.start_from({"toolCall": {"mode": "is-error", "delaySeconds": 1}, "toolsList": {"mode": "empty"}})
        self.assertEqual((error["advancedCount"], error["errorCount"]), ("기본값과 다른 설정 0개", "켜진 것 3개"))

        both = self.start_from({"session": {"validate": True}, "server": {"delayEnabled": True}})
        self.assertEqual((both["advancedCount"], both["errorCount"]), ("기본값과 다른 설정 1개", "켜진 것 1개"))

    def test_a_change_of_the_basic_settings_counts_in_neither_part(self) -> None:
        result = self.start_from(
            {"serverEra": "modern", "auth": {"mode": "oauth"}, "mcp": {"identifier": "my-server", "serviceName": "내 서비스"}}
        )
        self.assertTrue(result["chipsEmptyHidden"])
        self.assertEqual((result["advancedCount"], result["errorCount"]), ("기본값과 다른 설정 0개", "켜진 것 0개"))
        # Entering by a #cfg address fills the form without the notice of a load.
        self.assertEqual(result["loadStatus"], "")

    def test_an_address_put_in_fills_the_settings_and_says_which(self) -> None:
        patch = {"serverEra": "legacy", "allowedOrigins": ["https://app.example.com"], "toolCall": {"mode": "sse"}}
        config = normalize_config(patch)
        token = encode_config_token(config)
        result = self.start(
            render_scenario_page(),
            decode_body={"ok": True, "config": config, "token": token},
            paste=f"https://mcp.example.com/mcp?cfg={token}",
        )
        self.assertEqual(
            result["loadStatus"],
            "설정 3개를 채웠습니다: 지원 스펙, 허용할 Origin, tools/call 응답. 채운 항목은 아래에 '바꿈'으로 표시됩니다.",
        )
        self.assertEqual(result["loadError"], "")
        self.assertEqual(result["requests"], [{"url": "/scenario", "action": "decode"}])
        # The address the page makes now carries the same config, on this server.
        self.assertEqual(result["url"], f"http://localhost:8000/mcp?cfg={token}")
        self.assertTrue(result["chipsEmptyHidden"])
        self.assertEqual((result["advancedCount"], result["errorCount"]), ("기본값과 다른 설정 1개", "켜진 것 1개"))

    def test_a_pasted_config_json_fills_the_settings_too(self) -> None:
        patch = {"toolsList": {"pageSize": 2}, "toolCall": {"mode": "sse"}}
        config = normalize_config(patch)
        result = self.start(
            render_scenario_page(),
            decode_body={"ok": True, "config": config, "token": encode_config_token(config)},
            paste=json.dumps(patch),
        )
        self.assertEqual(
            result["loadStatus"],
            "설정 2개를 채웠습니다: 한 페이지의 툴 수, tools/call 응답. 채운 항목은 아래에 '바꿈'으로 표시됩니다.",
        )
        self.assertEqual(result["requests"], [{"url": "/scenario", "action": "validate"}])
        self.assertEqual((result["advancedCount"], result["errorCount"]), ("기본값과 다른 설정 1개", "켜진 것 1개"))

    def test_an_address_without_a_config_is_the_default_server(self) -> None:
        for address in ("https://mcp.example.com/mcp", "http://localhost:8000/mcp/", "/mcp", "https://h.example/a/mcp?x=1"):
            with self.subTest(address=address):
                result = self.start(render_scenario_page(), paste=address)
                self.assertEqual(
                    result["loadStatus"], "기본 설정 그대로인 주소입니다. 아래 항목은 모두 기본값입니다."
                )
                self.assertEqual(result["loadError"], "")
                # Nothing to ask the server: there is no config to read.
                self.assertEqual(result["requests"], [])
                self.assertEqual(result["url"], "http://localhost:8000/mcp")
                self.assertFalse(result["chipsEmptyHidden"])

    def test_what_cannot_be_read_is_said_in_plain_words(self) -> None:
        cases = (
            ("https://www.example.com/docs", {"ok": False, "error": "no cfg parameter in the URL"},
             "MCP 주소가 아닙니다. /mcp로 끝나는 주소나 설정(cfg=…)이 든 주소를 넣으세요. (no cfg parameter in the URL)"),
            ("https://mcp.example.com/mcp?cfg=broken", {"ok": False, "error": "invalid config token"},
             "주소나 설정 코드를 읽지 못했습니다. 이 서버에서 만든 주소가 아니거나 중간이 잘렸을 수 있습니다. "
             "(invalid config token)"),
            ('{"toolCall": ', None, None),
            ('{"noSuchSetting": 1}', None, "모르는 설정: noSuchSetting"),
        )
        for text, answer, expected in cases:
            with self.subTest(text=text):
                result = self.start(render_scenario_page(), decode_body=answer, paste=text)
                self.assertEqual(result["loadStatus"], "")
                if expected is None:
                    self.assertTrue(result["loadError"].startswith("JSON"), result["loadError"])
                else:
                    self.assertEqual(result["loadError"], expected)
                # The form stays as it was.
                self.assertEqual(result["url"], "http://localhost:8000/mcp")
                self.assertFalse(result["chipsEmptyHidden"])

    def test_start_from_a_hash_token(self) -> None:
        config = normalize_config({"serverEra": "legacy", "mcp": {"serviceName": "한글 서비스"}})
        token = encode_config_token(config)
        result = self.start(
            render_scenario_page(), f"#cfg={token}", {"ok": True, "config": config, "token": token}
        )
        self.assertEqual(result["url"], f"http://localhost:8000/mcp?cfg={token}")
        self.assertEqual(result["inspect"], f"/inspect?cfg={token}")
        self.assertEqual(result["token"], token)
        self.assertEqual(result["header"], f"X-MCP-Test-Scenario: cfg:{token}")
        self.assertEqual(result["hash"], f"#cfg={token}")
        self.assertTrue(result["chipsEmptyHidden"])
        self.assertEqual(result["loadError"], "")
        self.assertEqual(result["requests"][0], {"url": "/scenario", "action": "decode"})
        self.assertNotIn("/mcp", [request["url"] for request in result["requests"]])

    def test_a_bad_hash_token_shows_the_server_message(self) -> None:
        result = self.start(
            render_scenario_page(), "#cfg=%%%", {"ok": False, "error": "invalid config token"}
        )
        self.assertEqual(
            result["loadError"],
            "주소나 설정 코드를 읽지 못했습니다. 이 서버에서 만든 주소가 아니거나 중간이 잘렸을 수 있습니다. "
            "(invalid config token)",
        )
        self.assertEqual(result["url"], "http://localhost:8000/mcp")
        self.assertEqual(result["hash"], "")

    def test_an_unlisted_server_message_is_wrapped_in_korean_too(self) -> None:
        result = self.start(
            render_scenario_page(), "#cfg=abcd", {"ok": False, "error": "server.delaySeconds must be a number"}
        )
        self.assertEqual(
            result["loadError"],
            "설정을 불러오지 못했습니다. (server.delaySeconds must be a number)",
        )

    def test_changed_global_config_shows_the_banner_only(self) -> None:
        try:
            set_active_config({"toolCall": {"mode": "sse"}})
            page = render_scenario_page()
        finally:
            set_active_scenario("ok")
        result = self.start(page)
        self.assertFalse(result["bannerHidden"])
        self.assertEqual(
            result["banner"], "서버 기본값이 바뀌어 있습니다(모든 사용자에게 적용): tools/call 응답 = sse"
        )
        self.assertEqual(result["url"], "http://localhost:8000/mcp")
        self.assertFalse(result["chipsEmptyHidden"])


if __name__ == "__main__":
    unittest.main()
