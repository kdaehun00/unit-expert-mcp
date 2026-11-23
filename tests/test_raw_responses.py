"""Tests for developer-authored raw responses (config ``rawResponses``).

Rule matching/normalization is checked on the pure helpers; the wire behaviour
(status, headers, body framing, precedence over other checks) is checked against
a real Handler on an ephemeral port, like tests/test_oauth.py.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from unit_expert_mcp.server import (
    RAW_RESPONSE_MAX_BODY_LENGTH,
    RAW_RESPONSE_MAX_METHOD_LENGTH,
    RAW_RESPONSE_MAX_RULES,
    Handler,
    build_raw_response,
    decode_config_token,
    default_config,
    encode_config_token,
    inspect_snapshot,
    match_raw_response,
    normalize_config,
    normalize_raw_responses,
    render_raw_body,
    set_active_scenario,
)

MCP_ACCEPT = "application/json, text/event-stream"


def rule(method: str = "tools/list", **overrides) -> dict:
    base = {"method": method, "body": '{"jsonrpc":"2.0","id":"{{id}}","result":{}}'}
    base.update(overrides)
    return base


def config_with(*rules: dict, **overrides) -> dict:
    config = default_config()
    config["rawResponses"] = list(rules)
    config.update(overrides)
    return config


def request(method: str, request_id: object = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method}


class RawResponseNormalizeTest(unittest.TestCase):
    def test_default_config_has_no_rules(self) -> None:
        self.assertEqual(default_config()["rawResponses"], [])
        self.assertEqual(normalize_config({})["rawResponses"], [])

    def test_rule_defaults_are_filled_and_method_is_stripped(self) -> None:
        rules = normalize_config({"rawResponses": [{"method": "  tools/list  "}]})["rawResponses"]

        self.assertEqual(
            rules,
            [
                {
                    "enabled": True,
                    "method": "tools/list",
                    "status": 200,
                    "headers": {},
                    "contentType": "json",
                    "body": "",
                }
            ],
        )

    def test_unknown_rule_keys_are_dropped(self) -> None:
        rules = normalize_raw_responses([{"method": "*", "note": "ignored"}])

        self.assertNotIn("note", rules[0])

    def test_explicit_values_are_kept(self) -> None:
        raw = {
            "enabled": False,
            "method": "*",
            "status": 503,
            "headers": {"Retry-After": "3"},
            "contentType": "sse",
            "body": "not json",
        }

        self.assertEqual(normalize_raw_responses([raw]), [raw])

    def test_boundary_values_are_accepted(self) -> None:
        rules = normalize_raw_responses(
            [
                {"method": "a" * RAW_RESPONSE_MAX_METHOD_LENGTH, "status": 200},
                {"method": "*", "status": 599, "body": "x" * RAW_RESPONSE_MAX_BODY_LENGTH},
            ]
        )

        self.assertEqual([item["status"] for item in rules], [200, 599])
        normalize_raw_responses([{"method": "*"}] * RAW_RESPONSE_MAX_RULES)

    def test_invalid_rules_are_rejected(self) -> None:
        invalid = {
            "not a list": {"method": "*"},
            "too many rules": [{"method": "*"}] * (RAW_RESPONSE_MAX_RULES + 1),
            "rule not an object": ["tools/list"],
            "enabled not bool": [{"method": "*", "enabled": "yes"}],
            "method missing": [{"body": "{}"}],
            "method blank": [{"method": "   "}],
            "method not a string": [{"method": 1}],
            "method too long": [{"method": "a" * (RAW_RESPONSE_MAX_METHOD_LENGTH + 1)}],
            "status below 200": [{"method": "*", "status": 199}],
            "status above 599": [{"method": "*", "status": 600}],
            "status bool": [{"method": "*", "status": True}],
            "status string": [{"method": "*", "status": "200"}],
            "headers not an object": [{"method": "*", "headers": ["X-A: 1"]}],
            "header value not a string": [{"method": "*", "headers": {"X-A": 1}}],
            "header name empty": [{"method": "*", "headers": {"": "1"}}],
            "header name with space": [{"method": "*", "headers": {"X A": "1"}}],
            "header name with colon": [{"method": "*", "headers": {"X-A:": "1"}}],
            "header value with CR": [{"method": "*", "headers": {"X-A": "1\rX-B: 2"}}],
            "header value with LF": [{"method": "*", "headers": {"X-A": "1\nX-B: 2"}}],
            "contentType unknown": [{"method": "*", "contentType": "text"}],
            "body not a string": [{"method": "*", "body": {"jsonrpc": "2.0"}}],
            "body too long": [{"method": "*", "body": "x" * (RAW_RESPONSE_MAX_BODY_LENGTH + 1)}],
        }
        for label, raw_rules in invalid.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    normalize_config({"rawResponses": raw_rules})

    def test_error_message_names_the_offending_rule(self) -> None:
        with self.assertRaisesRegex(ValueError, r"rawResponses\[1\]\.status"):
            normalize_raw_responses([{"method": "*"}, {"method": "*", "status": 42}])

    def test_rules_survive_the_cfg_token_round_trip(self) -> None:
        config = config_with(rule("tools/call", status=500, headers={"X-A": "1"}, contentType="sse"))

        decoded = decode_config_token(encode_config_token(config))

        self.assertEqual(decoded["rawResponses"], normalize_config(config)["rawResponses"])


class RawResponseMatchTest(unittest.TestCase):
    def match(self, rules: list[dict], payload: object) -> dict | None:
        return match_raw_response(normalize_config({"rawResponses": rules}), payload)

    def test_no_rules_means_no_match(self) -> None:
        self.assertIsNone(match_raw_response(default_config(), request("tools/list")))

    def test_exact_method_matches(self) -> None:
        matched = self.match([rule("tools/list", body="A")], request("tools/list"))

        assert matched is not None
        self.assertEqual(matched["body"], "A")

    def test_other_method_does_not_match_without_wildcard(self) -> None:
        self.assertIsNone(self.match([rule("tools/list")], request("tools/call")))

    def test_wildcard_matches_any_method(self) -> None:
        matched = self.match([rule("*", body="W")], request("server/discover"))

        assert matched is not None
        self.assertEqual(matched["body"], "W")

    def test_exact_match_wins_over_earlier_wildcard(self) -> None:
        rules = [rule("*", body="W"), rule("tools/list", body="A")]

        exact = self.match(rules, request("tools/list"))
        fallback = self.match(rules, request("tools/call"))

        assert exact is not None and fallback is not None
        self.assertEqual(exact["body"], "A")
        self.assertEqual(fallback["body"], "W")

    def test_first_rule_wins_among_duplicates(self) -> None:
        exact = self.match(
            [rule("tools/list", body="first"), rule("tools/list", body="second")],
            request("tools/list"),
        )
        wildcard = self.match(
            [rule("*", body="first"), rule("*", body="second")], request("ping")
        )

        assert exact is not None and wildcard is not None
        self.assertEqual(exact["body"], "first")
        self.assertEqual(wildcard["body"], "first")

    def test_disabled_rules_are_skipped(self) -> None:
        rules = [
            rule("tools/list", body="off", enabled=False),
            rule("*", body="W-off", enabled=False),
            rule("*", body="W"),
        ]

        matched = self.match(rules, request("tools/list"))

        assert matched is not None
        self.assertEqual(matched["body"], "W")
        self.assertIsNone(self.match(rules[:2], request("tools/list")))

    def test_method_match_is_case_sensitive(self) -> None:
        self.assertIsNone(self.match([rule("tools/list")], request("Tools/List")))

    def test_payload_without_string_method_never_matches(self) -> None:
        rules = [rule("*")]

        for payload in ([request("tools/list")], "tools/list", None, {"id": 1}, {"method": 7}):
            with self.subTest(payload=payload):
                self.assertIsNone(self.match(rules, payload))

    def test_notification_matches_by_method(self) -> None:
        payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}

        self.assertIsNotNone(self.match([rule("notifications/initialized")], payload))


class RawResponseRenderTest(unittest.TestCase):
    BODY = '{"jsonrpc":"2.0","id":"{{id}}","result":{}}'

    def test_placeholder_becomes_json_encoded_id(self) -> None:
        cases = {
            7: '{"jsonrpc":"2.0","id":7,"result":{}}',
            "abc": '{"jsonrpc":"2.0","id":"abc","result":{}}',
            'a"b': '{"jsonrpc":"2.0","id":"a\\"b","result":{}}',
            "한글": '{"jsonrpc":"2.0","id":"한글","result":{}}',
            None: '{"jsonrpc":"2.0","id":null,"result":{}}',
        }
        for request_id, expected in cases.items():
            with self.subTest(request_id=request_id):
                self.assertEqual(render_raw_body(self.BODY, request_id), expected)

    def test_every_occurrence_is_replaced(self) -> None:
        self.assertEqual(render_raw_body('["{{id}}","{{id}}"]', 3), "[3,3]")

    def test_unquoted_placeholder_is_left_alone(self) -> None:
        self.assertEqual(render_raw_body('{"id":{{id}}}', 3), '{"id":{{id}}}')

    def test_body_without_placeholder_is_unchanged(self) -> None:
        self.assertEqual(render_raw_body("<<broken", 3), "<<broken")

    def test_json_body_is_sent_verbatim_with_json_headers(self) -> None:
        raw = normalize_raw_responses([rule(body="<<한글")])[0]

        body, headers = build_raw_response(raw, raw["body"])

        self.assertEqual(body, "<<한글".encode("utf-8"))
        self.assertEqual(
            headers, {"Content-Type": "application/json", "Content-Length": str(len(body))}
        )

    def test_sse_body_is_framed_as_one_message_event(self) -> None:
        raw = normalize_raw_responses([rule(contentType="sse")])[0]

        body, headers = build_raw_response(raw, '{"a":1}\n{"b":2}')

        self.assertEqual(body, b'event: message\ndata: {"a":1}\ndata: {"b":2}\n\n')
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Content-Length"], str(len(body)))

    def test_empty_sse_body_still_emits_a_data_line(self) -> None:
        raw = normalize_raw_responses([rule(contentType="sse", body="")])[0]

        body, _ = build_raw_response(raw, "")

        self.assertEqual(body, b"event: message\ndata: \n\n")

    def test_user_headers_override_defaults_case_insensitively(self) -> None:
        raw = normalize_raw_responses(
            [
                rule(
                    contentType="sse",
                    headers={"content-type": "text/plain", "CACHE-CONTROL": "max-age=1", "X-A": "1"},
                )
            ]
        )[0]

        _, headers = build_raw_response(raw, "x")

        self.assertNotIn("Content-Type", headers)
        self.assertNotIn("Cache-Control", headers)
        self.assertEqual(headers["content-type"], "text/plain")
        self.assertEqual(headers["CACHE-CONTROL"], "max-age=1")
        self.assertEqual(headers["X-A"], "1")

    def test_user_content_length_is_ignored(self) -> None:
        raw = normalize_raw_responses([rule(headers={"content-length": "999"})])[0]

        body, headers = build_raw_response(raw, "abc")

        self.assertEqual(body, b"abc")
        self.assertEqual(headers, {"Content-Type": "application/json", "Content-Length": "3"})

    def test_bodyless_statuses_send_no_body(self) -> None:
        for status in (204, 304):
            with self.subTest(status=status):
                raw = normalize_raw_responses([rule(status=status, body="ignored")])[0]

                body, headers = build_raw_response(raw, "ignored")

                self.assertEqual(body, b"")
                self.assertEqual(headers["Content-Length"], "0")


class RawResponseHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def tearDown(self) -> None:
        set_active_scenario("ok")

    def raw_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        response_headers = [(key.lower(), value) for key, value in response.getheaders()]
        conn.close()
        return response.status, response_headers, raw

    def post(
        self,
        config: dict | None,
        payload: object,
        *,
        headers: dict[str, str] | None = None,
        accept: str | None = MCP_ACCEPT,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = {"Content-Type": "application/json"}
        if accept is not None:
            request_headers["Accept"] = accept
        request_headers.update(headers or {})
        path = "/mcp" if config is None else f"/mcp?cfg={encode_config_token(config)}"
        status, response_headers, raw = self.raw_request(
            "POST", path, headers=request_headers, body=json.dumps(payload).encode("utf-8")
        )
        return status, dict(response_headers), raw

    # -- response shape -------------------------------------------------------
    def test_json_rule_answers_with_substituted_id(self) -> None:
        status, headers, raw = self.post(config_with(rule("tools/list")), request("tools/list", 42))

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["content-length"], str(len(raw)))
        self.assertEqual(raw, b'{"jsonrpc":"2.0","id":42,"result":{}}')

    def test_string_id_and_missing_id_are_substituted(self) -> None:
        config = config_with(rule("*"))

        _, _, with_string = self.post(config, request("ping", "req-1"))
        _, _, without_id = self.post(config, {"jsonrpc": "2.0", "method": "ping"})

        self.assertEqual(json.loads(with_string)["id"], "req-1")
        self.assertIsNone(json.loads(without_id)["id"])

    def test_broken_json_body_is_sent_verbatim(self) -> None:
        status, headers, raw = self.post(
            config_with(rule("tools/list", body='{"jsonrpc":"2.0",')), request("tools/list")
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(raw, b'{"jsonrpc":"2.0",')

    def test_sse_rule_frames_the_body(self) -> None:
        status, headers, raw = self.post(
            config_with(rule("tools/list", contentType="sse")), request("tools/list", 5)
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(raw, b'event: message\ndata: {"jsonrpc":"2.0","id":5,"result":{}}\n\n')

    def test_status_and_headers_are_applied(self) -> None:
        status, headers, raw = self.post(
            config_with(
                rule(
                    "tools/call",
                    status=503,
                    headers={"Retry-After": "7", "Content-Type": "text/plain", "Content-Length": "1"},
                    body="down",
                )
            ),
            request("tools/call"),
        )

        self.assertEqual(status, 503)
        self.assertEqual(headers["retry-after"], "7")
        self.assertEqual(headers["content-type"], "text/plain")
        self.assertEqual(headers["content-length"], "4")
        self.assertEqual(raw, b"down")

    def test_204_rule_has_no_body(self) -> None:
        status, headers, raw = self.post(
            config_with(rule("tools/list", status=204, body="ignored")), request("tools/list")
        )

        self.assertEqual(status, 204)
        self.assertEqual(raw, b"")
        self.assertEqual(headers["content-length"], "0")

    def test_cors_headers_are_still_sent(self) -> None:
        _, headers, _ = self.post(config_with(rule("*")), request("ping"))

        self.assertEqual(headers["access-control-allow-origin"], "*")

    def test_user_cors_headers_replace_the_defaults(self) -> None:
        status, headers, _ = self.raw_request(
            "POST",
            "/mcp?cfg="
            + encode_config_token(
                config_with(
                    rule(
                        "*",
                        headers={
                            "access-control-allow-origin": "https://example.test",
                            "Vary": "Accept",
                        },
                    )
                )
            ),
            headers={
                "Content-Type": "application/json",
                "Accept": MCP_ACCEPT,
                "Origin": "https://caller.test",
            },
            body=json.dumps(request("ping")).encode("utf-8"),
        )

        self.assertEqual(status, 200)
        names = [name for name, _ in headers]
        self.assertEqual(names.count("access-control-allow-origin"), 1)
        self.assertEqual(names.count("vary"), 1)
        values = dict(headers)
        self.assertEqual(values["access-control-allow-origin"], "https://example.test")
        self.assertEqual(values["vary"], "Accept")
        # Defaults the user did not name are still sent once.
        self.assertEqual(names.count("access-control-allow-methods"), 1)
        self.assertEqual(names.count("access-control-expose-headers"), 1)

    # -- matching over HTTP ---------------------------------------------------
    def test_exact_rule_beats_wildcard_and_unmatched_falls_back(self) -> None:
        config = config_with(rule("*", body="W"), rule("tools/list", body="A"))

        _, _, exact = self.post(config, request("tools/list"))
        _, _, fallback = self.post(config, request("initialize"))

        self.assertEqual(exact, b"A")
        self.assertEqual(fallback, b"W")

    def test_unmatched_method_gets_the_normal_response(self) -> None:
        status, headers, raw = self.post(config_with(rule("tools/call", body="A")), request("tools/list"))

        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)["result"]["tools"]), 6)

    def test_disabled_rule_gets_the_normal_response(self) -> None:
        status, _, raw = self.post(
            config_with(rule("tools/list", body="A", enabled=False)), request("tools/list")
        )

        self.assertEqual(status, 200)
        self.assertIn("tools", json.loads(raw)["result"])

    # -- precedence over the other checks --------------------------------------
    def test_answers_before_accept_validation(self) -> None:
        config = config_with(rule("tools/list", body="A"))

        status, _, raw = self.post(config, request("tools/list"), accept=None)
        blocked_status, _, _ = self.post(default_config(), request("tools/list"), accept=None)

        self.assertEqual((status, raw), (200, b"A"))
        self.assertEqual(blocked_status, 400)

    def test_answers_before_http_status_injection(self) -> None:
        config = config_with(rule("tools/list", body="A"))
        config["server"]["httpStatus"] = 401

        status, _, raw = self.post(config, request("tools/list"))
        other_status, _, _ = self.post(config, request("tools/call"))

        self.assertEqual((status, raw), (200, b"A"))
        self.assertEqual(other_status, 401)

    def test_answers_before_custom_header_check(self) -> None:
        config = config_with(rule("tools/list", body="A"))
        config["customHeader"]["enabled"] = True

        status, _, raw = self.post(config, request("tools/list"))

        self.assertEqual((status, raw), (200, b"A"))

    def test_answers_before_oauth(self) -> None:
        config = config_with(rule("tools/list", body="A"))
        config["auth"]["mode"] = "oauth"

        status, headers, raw = self.post(config, request("tools/list"))
        other_status, _, _ = self.post(config, request("tools/call"))

        self.assertEqual((status, raw), (200, b"A"))
        self.assertNotIn("www-authenticate", headers)
        self.assertEqual(other_status, 401)

    def test_answers_before_reject_legacy(self) -> None:
        config = config_with(rule("initialize", body="A"), rejectLegacy=True)

        status, _, raw = self.post(config, request("initialize"))
        other_status, _, other = self.post(config, request("tools/list"))

        self.assertEqual((status, raw), (200, b"A"))
        self.assertEqual(other_status, 400)
        self.assertEqual(json.loads(other)["error"]["code"], -32022)

    def test_answers_before_2026_validation(self) -> None:
        config = config_with(rule("tools/list", body="A"))
        # Mcp-Method routes to the 2026 path, where the missing _meta would be a 400.
        mismatched = {"Mcp-Method": "tools/call"}

        status, _, raw = self.post(config, request("tools/list"), headers=mismatched)
        plain_status, _, _ = self.post(default_config(), request("tools/list"), headers=mismatched)

        self.assertEqual((status, raw), (200, b"A"))
        self.assertEqual(plain_status, 400)

    def test_answers_without_the_configured_delay(self) -> None:
        config = config_with(rule("tools/list", body="A"))
        config["server"]["delayEnabled"] = True
        config["server"]["delaySeconds"] = 3

        started = time.monotonic()
        status, _, raw = self.post(config, request("tools/list"))
        elapsed = time.monotonic() - started

        self.assertEqual((status, raw), (200, b"A"))
        self.assertLess(elapsed, 2)

    # -- config plumbing --------------------------------------------------------
    def test_invalid_rule_in_cfg_token_is_a_400(self) -> None:
        bad = base64.urlsafe_b64encode(
            json.dumps({"rawResponses": [{"method": "*", "status": 99}]}).encode("utf-8")
        ).decode("ascii").rstrip("=")

        status, _, raw = self.raw_request(
            "POST",
            f"/mcp?cfg={bad}",
            headers={"Content-Type": "application/json", "Accept": MCP_ACCEPT},
            body=json.dumps(request("tools/list")).encode("utf-8"),
        )

        self.assertEqual(status, 400)
        self.assertIn(b"rawResponses[0].status", raw)

    def test_active_config_set_through_scenario_endpoint_applies(self) -> None:
        body = json.dumps({"config": config_with(rule("tools/list", body="A"))}).encode("utf-8")

        status, _, raw = self.raw_request(
            "POST", "/scenario", headers={"Content-Type": "application/json"}, body=body
        )
        applied = json.loads(raw)

        self.assertEqual(status, 200)
        self.assertEqual(applied["config"]["rawResponses"][0]["method"], "tools/list")
        self.assertEqual(self.post(None, request("tools/list"))[2], b"A")

    def test_scenario_endpoint_rejects_invalid_rules(self) -> None:
        body = json.dumps({"config": {"rawResponses": [{"method": ""}]}}).encode("utf-8")

        status, _, raw = self.raw_request(
            "POST", "/scenario", headers={"Content-Type": "application/json"}, body=body
        )

        self.assertEqual(status, 400)
        self.assertIn("rawResponses[0].method", json.loads(raw)["error"])

    def test_exchange_is_recorded_as_raw_in_the_inspect_log(self) -> None:
        marker = f"raw-inspect-{time.monotonic_ns()}"
        config = config_with(rule("tools/list", status=418, headers={"X-A": "1"}))

        self.post(config, request("tools/list", marker))
        entry = next(
            item
            for item in inspect_snapshot()
            if isinstance(item["requestBody"], dict) and item["requestBody"].get("id") == marker
        )

        self.assertIs(entry["rawResponse"], True)
        self.assertEqual(entry["status"], 418)
        self.assertEqual(entry["responseBody"], {"jsonrpc": "2.0", "id": marker, "result": {}})
        self.assertEqual(entry["responseHeaders"]["X-A"], "1")
        self.assertEqual(entry["responseHeaders"]["Content-Type"], "application/json")


if __name__ == "__main__":
    unittest.main()
