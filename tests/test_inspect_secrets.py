"""Credentials in the /inspect log: masked before they are stored unless
MCP_INSPECT_RAW_SECRETS is on, without changing what /mcp answers."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import unittest
from typing import Any
from unittest import mock

from test_inspect_log import InspectLogTestCase
from unit_expert_mcp import server as server_module
from unit_expert_mcp.server import (
    INSPECT_RAW_SECRETS_ENV,
    OAUTH_SCOPE,
    UI_CORE_JS,
    inspect_raw_secrets,
    inspect_secrets_mode,
    is_secret_header,
    issue_access_token,
    mask_secret_headers,
    mask_secret_query,
    mask_secret_value,
    record_exchange,
    render_inspect_page,
    secret_fingerprint,
)

from mcp_test_support import legacy_request

LONG = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"  # 43 characters
OAUTH = {"auth": {"mode": "oauth"}}


def raw_mode(value: str = "1"):
    return mock.patch.dict(os.environ, {INSPECT_RAW_SECRETS_ENV: value})


def masked_mode():
    environ = {name: value for name, value in os.environ.items() if name != INSPECT_RAW_SECRETS_ENV}
    return mock.patch.dict(os.environ, environ, clear=True)


class SecretHeaderNamesTest(unittest.TestCase):
    def test_credential_headers(self) -> None:
        for name in (
            "Authorization",
            "authorization",
            "AUTHORIZATION",
            "Proxy-Authorization",
            "Cookie",
            "Set-Cookie",
            "X-Api-Key",
            "x-apikey",
            "X-Api_Key",
            "X-Auth-Token",
            "X-MCP-Token",
            "X-Client-Secret",
            "X-Password",
            "X-Forwarded-Authorization",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_secret_header(name))

    def test_headers_that_are_not_credentials(self) -> None:
        for name in (
            "Accept",
            "Content-Type",
            "WWW-Authenticate",
            "MCP-Protocol-Version",
            "Mcp-Method",
            "Mcp-Name",
            "Mcp-Session-Id",
            "Mcp-Param-Token",
            "mcp-param-secret",
            "X-MCP-Test-Scenario",
            "X-Unit-Expert-Source",
            "Allow",
        ):
            with self.subTest(name=name):
                self.assertFalse(is_secret_header(name))

    def test_the_screen_uses_the_same_lists(self) -> None:
        for name in server_module.SECRET_HEADER_NAMES + server_module.SECRET_HEADER_EXEMPT_NAMES:
            self.assertIn(json.dumps(name), UI_CORE_JS)
        self.assertIn(json.dumps(server_module.SECRET_NAME_RE.pattern), UI_CORE_JS)
        self.assertIn(json.dumps(list(server_module.AUTH_SCHEMES)), UI_CORE_JS)


class MaskValueTest(unittest.TestCase):
    def test_a_long_value_keeps_scheme_tail_length_and_fingerprint(self) -> None:
        masked = mask_secret_value(f"Bearer {LONG}", scheme=True)
        self.assertEqual(masked, f"Bearer ***DEFG (len 43, fp {secret_fingerprint(LONG)})")
        self.assertRegex(masked, r"^Bearer \*\*\*DEFG \(len 43, fp [0-9a-f]{8}\)$")
        self.assertNotIn(LONG[:-4], masked)

    def test_a_short_value_keeps_nothing(self) -> None:
        self.assertEqual(mask_secret_value("Bearer 12345678", scheme=True), "Bearer ***")
        self.assertEqual(mask_secret_value("Bearer x", scheme=True), "Bearer ***")
        self.assertEqual(mask_secret_value("12345678"), "***")
        self.assertRegex(mask_secret_value("123456789"), r"^\*\*\*6789 \(len 9, fp [0-9a-f]{8}\)$")

    def test_scheme_is_kept_only_when_known(self) -> None:
        self.assertTrue(mask_secret_value(f"bearer {LONG}", scheme=True).startswith("bearer ***DEFG"))
        self.assertTrue(mask_secret_value(f"Basic {LONG}", scheme=True).startswith("Basic ***DEFG"))
        # An unknown first word may be part of the credential.
        odd = mask_secret_value(f"my-secret-word {LONG}", scheme=True)
        self.assertTrue(odd.startswith("***DEFG (len 58"))
        self.assertNotIn("my-secret-word", odd)
        # Without scheme=True nothing in front stays.
        self.assertTrue(mask_secret_value(f"Bearer {LONG}").startswith("***DEFG (len 50"))
        # A value without a scheme is all credential.
        self.assertTrue(mask_secret_value(LONG, scheme=True).startswith("***DEFG (len 43"))

    def test_values_without_a_credential_stay(self) -> None:
        self.assertEqual(mask_secret_value("Bearer", scheme=True), "Bearer")
        self.assertEqual(mask_secret_value("Bearer ", scheme=True), "Bearer ")
        self.assertEqual(mask_secret_value(""), "")
        self.assertIsNone(mask_secret_value(None))

    def test_fingerprint_tells_equal_values_apart_from_others(self) -> None:
        self.assertEqual(secret_fingerprint(LONG), secret_fingerprint(LONG))
        self.assertNotEqual(secret_fingerprint(LONG), secret_fingerprint(LONG + "x"))
        self.assertEqual(
            mask_secret_value(f"Bearer {LONG}", scheme=True), mask_secret_value(f"Bearer   {LONG} ", scheme=True)
        )
        # Keyed with a per-process secret: not the plain SHA-256 of the value.
        import hashlib

        self.assertNotEqual(secret_fingerprint(LONG), hashlib.sha256(LONG.encode()).hexdigest()[:8])

    def test_headers(self) -> None:
        headers = {
            "Authorization": f"Bearer {LONG}",
            "Cookie": "sid=1234567890abcdef",
            "X-Api-Key": "k",
            "Set-Cookie": "sid=1234567890abcdef; Path=/",
            "WWW-Authenticate": 'Bearer resource_metadata="http://h/x"',
            "Mcp-Param-Token": "plain-argument-value",
            "X-MCP-Test-Scenario": "cfg:eyJhIjoxfQ",
            "Accept": "application/json",
        }
        masked = mask_secret_headers(headers)
        self.assertEqual(list(masked), list(headers))
        self.assertTrue(masked["Authorization"].startswith("Bearer ***DEFG (len 43, fp "))
        self.assertTrue(masked["Cookie"].startswith("***cdef (len 20, fp "))
        self.assertEqual(masked["X-Api-Key"], "***")
        self.assertTrue(masked["Set-Cookie"].startswith("***th=/ (len 28, fp "))
        for name in ("WWW-Authenticate", "Mcp-Param-Token", "X-MCP-Test-Scenario", "Accept"):
            self.assertEqual(masked[name], headers[name])
        self.assertEqual(headers["Authorization"], f"Bearer {LONG}")  # the argument is not changed

    def test_query(self) -> None:
        self.assertEqual(mask_secret_query(""), "")
        self.assertEqual(mask_secret_query("cfg=eyJhIjoxfQ"), "cfg=eyJhIjoxfQ")
        self.assertEqual(
            mask_secret_query(f"cfg=eyJhIjoxfQ&access_token={LONG}&x=1&token=t&Api_Key=k&client%5Fsecret=s&token="),
            "cfg=eyJhIjoxfQ&access_token=***&x=1&token=***&Api_Key=***&client%5Fsecret=***&token=",
        )


class RecordedSecretsTest(InspectLogTestCase):
    """What record_exchange stores, over HTTP and called directly."""

    def setUp(self) -> None:
        super().setUp()
        patcher = masked_mode()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_is_masked(self) -> None:
        self.assertFalse(inspect_raw_secrets())
        self.assertEqual(inspect_secrets_mode(), "masked")

    def test_request_header_and_query_are_masked(self) -> None:
        token = "only-in-this-test-" + LONG[:21] + "DEFG"  # 43 characters
        result = self.recorded_post(
            legacy_request("tools/list"),
            headers={"Authorization": f"Bearer {token}", "X-MCP-Token": token, "Mcp-Param-Token": token[:10]},
            path=f"/mcp?access_token={token}&keep=1",
        )
        self.assertEqual(result[0], 200)
        entry = result[3][0]
        self.assertTrue(entry["requestHeaders"]["Authorization"].startswith("Bearer ***DEFG (len 43, fp "))
        self.assertTrue(entry["requestHeaders"]["X-MCP-Token"].startswith("***DEFG (len 43, fp "))
        self.assertEqual(entry["requestHeaders"]["Mcp-Param-Token"], token[:10])
        self.assertEqual(entry["query"], "access_token=***&keep=1")
        self.assertEqual(entry["requestHeaders"]["Accept"], "application/json, text/event-stream")
        self.assertNotIn(token, json.dumps(entry))
        status, _, raw = self.fetch("GET", "/inspect/log")
        self.assertEqual(status, 200)
        self.assertNotIn(token.encode(), raw)
        self.assertEqual(json.loads(raw)["secrets"], "masked")

    def test_cfg_is_recorded_as_sent(self) -> None:
        entry = self.recorded_post(legacy_request("tools/list"), config={"toolsList": {"mode": "empty"}})[3][0]
        self.assertEqual(entry["query"], "cfg=" + entry["cfg"])
        self.assertEqual("/mcp?" + entry["query"], self.path({"toolsList": {"mode": "empty"}}))

    def test_the_body_is_not_masked(self) -> None:
        payload = legacy_request("tools/call", name="convert_length", arguments={"token": LONG})
        entry = self.recorded_post(payload)[3][0]
        self.assertEqual(entry["requestBody"], payload)

    def test_same_token_same_fingerprint(self) -> None:
        def recorded(token: str) -> str:
            headers = {"Authorization": f"Bearer {token}"}
            return self.recorded_post(legacy_request("tools/list"), headers=headers)[3][0]["requestHeaders"]["Authorization"]

        self.assertEqual(recorded(LONG), recorded(LONG))
        self.assertNotEqual(recorded(LONG), recorded("X" + LONG[1:]))

    def test_response_headers(self) -> None:
        with self.subTest("a rawResponses rule: the user's own values stay, Set-Cookie is masked"):
            rule = {
                "method": "tools/list",
                "body": '{"jsonrpc":"2.0","id":"{{id}}","result":{"token":"' + LONG + '"}}',
                "headers": {"X-Custom-Token": LONG, "Set-Cookie": f"sid={LONG}; Path=/"},
            }
            status, headers, raw, entries = self.recorded_post(
                legacy_request("tools/list"), config={"rawResponses": [rule]}
            )
            self.assertEqual(status, 200)
            # The response on the wire is what the rule says.
            self.assertEqual(headers["x-custom-token"], LONG)
            self.assertEqual(headers["set-cookie"], f"sid={LONG}; Path=/")
            self.assertIn(LONG.encode(), raw)
            recorded = entries[0]["responseHeaders"]
            self.assertTrue(recorded["Set-Cookie"].startswith("***th=/ (len "))
            self.assertEqual(entries[0]["responseBody"]["result"]["token"], LONG)
        with self.subTest("WWW-Authenticate is not a credential"):
            challenge = 'Bearer resource_metadata="http://h/x"'
            entry = record_exchange(
                "POST", "/mcp", {}, None, 401, None, response_headers={"WWW-Authenticate": challenge}
            )
            self.assertEqual(entry["responseHeaders"], {"WWW-Authenticate": challenge})

    def test_raw_response_user_headers_are_recorded_as_written(self) -> None:
        entry = record_exchange(
            "POST", "/mcp", {}, None, 200, "{}", raw_response=True, response_headers={"X-Custom": LONG}
        )
        self.assertEqual(entry["responseHeaders"], {"X-Custom": LONG})


class OptInRawTest(InspectLogTestCase):
    def test_values_that_turn_it_on(self) -> None:
        for value, expected in (
            ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True), (" 1 ", True),
            ("", False), ("0", False), ("false", False), ("no", False), ("off", False), ("raw", False),
        ):
            with self.subTest(value=value), raw_mode(value):
                self.assertEqual(inspect_raw_secrets(), expected)
                self.assertEqual(inspect_secrets_mode(), "raw" if expected else "masked")

    def test_raw_mode_records_as_received(self) -> None:
        with raw_mode():
            entry = self.recorded_post(
                legacy_request("tools/list"),
                headers={"Authorization": f"Bearer {LONG}"},
                path=f"/mcp?access_token={LONG}",
            )[3][0]
            status, _, raw = self.fetch("GET", "/inspect/log?limit=1")
        self.assertEqual(entry["requestHeaders"]["Authorization"], f"Bearer {LONG}")
        self.assertEqual(entry["query"], f"access_token={LONG}")
        body = json.loads(raw)
        self.assertEqual(list(body), ["exchanges", "secrets"])
        self.assertEqual(body["secrets"], "raw")

    def test_an_error_answer_has_no_new_field(self) -> None:
        status, _, raw = self.fetch("GET", "/inspect/log?since=x")
        self.assertEqual(status, 400)
        self.assertEqual(set(json.loads(raw)), {"ok", "error"})

    def test_startup_warning(self) -> None:
        class Stop(Exception):
            pass

        class FakeServer:
            def __init__(self, *args: Any) -> None:
                pass

            def serve_forever(self) -> None:
                raise Stop

        def start() -> str:
            out = io.StringIO()
            with mock.patch.object(server_module, "ThreadingHTTPServer", FakeServer):
                with contextlib.redirect_stdout(out), self.assertRaises(Stop):
                    server_module.main()
            return out.getvalue()

        with raw_mode():
            lines = start().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith(f"WARNING: {INSPECT_RAW_SECRETS_ENV} is on."))
        with masked_mode():
            self.assertEqual(len(start().splitlines()), 1)

    def test_deploy_config_does_not_turn_it_on(self) -> None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("render.yaml", "Dockerfile", "Dockerfile.sdk2026"):
            path = os.path.join(root, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as handle:
                    self.assertNotIn(INSPECT_RAW_SECRETS_ENV, handle.read(), name)


class AuthIsUnchangedTest(InspectLogTestCase):
    """The token is checked as received; only the log holds the masked value."""

    def check(self) -> None:
        good = issue_access_token({"sub": "u", "client_id": "c", "scope": OAUTH_SCOPE})
        bad = good[:-6] + ("AAAAAA" if not good.endswith("AAAAAA") else "BBBBBB")
        ok = self.recorded_post(legacy_request("tools/list"), config=OAUTH, headers={"Authorization": f"Bearer {good}"})
        self.assertEqual(ok[0], 200)
        self.assertIn("tools", json.loads(ok[2])["result"])
        self.assertEqual(ok[3][0]["injected"], [])
        refused = self.recorded_post(
            legacy_request("tools/list"), config=OAUTH, headers={"Authorization": f"Bearer {bad}"}
        )
        self.assertEqual(refused[0], 401)
        self.assertIn("www-authenticate", refused[1])
        self.assertEqual(json.loads(refused[2])["error"], "invalid_token")
        self.assertEqual(refused[3][0]["injected"], ["auth.mode=oauth"])
        missing = self.recorded_post(legacy_request("tools/list"), config=OAUTH)
        self.assertEqual(missing[0], 401)
        self.assertNotIn("Authorization", missing[3][0]["requestHeaders"])
        return good, bad, ok[3][0], refused[3][0]

    def test_masked(self) -> None:
        with masked_mode():
            good, bad, ok, refused = self.check()
            log = self.fetch("GET", "/inspect/log")[2].decode("utf-8")
        for token, entry in ((good, ok), (bad, refused)):
            recorded = entry["requestHeaders"]["Authorization"]
            self.assertRegex(recorded, r"^Bearer \*\*\*.{4} \(len \d+, fp [0-9a-f]{8}\)$")
            self.assertIn(f"(len {len(token)},", recorded)
            self.assertNotIn(token, log)
            # Not even a long piece of it.
            self.assertNotIn(token[:20], log)
            self.assertNotIn(token[-12:], log)
        self.assertNotEqual(ok["requestHeaders"]["Authorization"], refused["requestHeaders"]["Authorization"])

    def test_raw(self) -> None:
        with raw_mode():
            good, bad, ok, refused = self.check()
        self.assertEqual(ok["requestHeaders"]["Authorization"], f"Bearer {good}")
        self.assertEqual(refused["requestHeaders"]["Authorization"], f"Bearer {bad}")


class ScreenCorsTest(InspectLogTestCase):
    """The pages and their JSON are never readable with another origin's credentials."""

    ORIGIN = "https://other.example"
    SCREEN = ("/", "/scenario", "/scenario/meta", "/inspect", "/inspect/log", "/send")

    def cors(self, method: str, path: str, **headers: str) -> dict[str, str]:
        return self.fetch(method, path, {"Origin": self.ORIGIN, **headers})[1]

    def test_screen_paths_do_not_allow_credentials(self) -> None:
        for path in self.SCREEN:
            for method in ("GET", "OPTIONS"):
                with self.subTest(path=path, method=method):
                    headers = self.cors(method, path)
                    self.assertNotIn("access-control-allow-credentials", headers)
                    # Still readable without credentials, as before.
                    self.assertEqual(headers["access-control-allow-origin"], self.ORIGIN)
        with self.subTest("query string"):
            self.assertNotIn("access-control-allow-credentials", self.cors("GET", "/inspect/log?limit=1"))
        with self.subTest("POST /scenario"):
            status, headers, _ = self.fetch(
                "POST",
                "/scenario",
                {"Origin": self.ORIGIN, "Content-Type": "application/json"},
                json.dumps({"action": "normalize", "config": {}}).encode("utf-8"),
            )
            self.assertIn(status, (200, 400))
            self.assertEqual(headers["access-control-allow-origin"], self.ORIGIN)
            self.assertNotIn("access-control-allow-credentials", headers)

    def test_mcp_cors_is_unchanged(self) -> None:
        expected = {
            "access-control-allow-origin": self.ORIGIN,
            "access-control-allow-credentials": "true",
            "access-control-allow-methods": "GET, POST, OPTIONS, DELETE",
            "access-control-expose-headers": "Mcp-Session-Id,mcp-session-id,*",
            "vary": "Origin",
        }
        preflight = self.cors("OPTIONS", "/mcp", **{"Access-Control-Request-Headers": "authorization,mcp-method"})
        self.assertEqual({name: preflight[name] for name in expected}, expected)
        self.assertEqual(preflight["access-control-allow-headers"], "authorization,mcp-method")
        status, headers, _, _ = self.recorded_post(legacy_request("tools/list"), headers={"Origin": self.ORIGIN})
        self.assertEqual(status, 200)
        self.assertEqual({name: headers[name] for name in expected}, expected)

    def test_other_paths_are_unchanged(self) -> None:
        for path in ("/healthz", "/.well-known/oauth-protected-resource"):
            with self.subTest(path=path):
                self.assertEqual(self.cors("GET", path)["access-control-allow-credentials"], "true")


class InspectPageNoticeTest(unittest.TestCase):
    START = "최근 200건을 모든 사용자가 함께 봅니다. "

    def notice(self) -> str:
        found = re.search(r'<p class="caution" id="caution" role="note">([^<]*)</p>', render_inspect_page())
        assert found is not None
        return found.group(1)

    def test_masked(self) -> None:
        with masked_mode():
            page = render_inspect_page()
            self.assertEqual(self.notice(), self.START + "인증 헤더 값은 가려서 기록하고, 바디는 그대로 남습니다.")
        self.assertIn('"secrets": "masked"', page)

    def test_raw(self) -> None:
        with raw_mode():
            page = render_inspect_page()
            self.assertEqual(self.notice(), self.START + "요청 헤더가 그대로 남으니 실제 서비스 토큰은 보내지 마세요.")
        self.assertIn('"secrets": "raw"', page)

    def test_the_reveal_button_is_for_raw_records_only(self) -> None:
        page = render_inspect_page()
        section = page[page.index("function requestSection(x)"):page.index("function responseSection(x)")]
        masked_branch = section.index('if (hasSecret && secretsMode !== "raw")')
        reveal = section.index('text: "값 보기"')
        self.assertLess(masked_branch, section.index("서버가 가려서 기록했습니다."))
        self.assertLess(section.index("} else if (hasSecret) {"), reveal)


if __name__ == "__main__":
    unittest.main()
