"""The /inspect log: every /mcp response is recorded once, with the fields the
inspection page needs, and GET /inspect/log can filter it."""

from __future__ import annotations

import json
import re
import time
from http.client import HTTPConnection
from typing import Any

from mcp_test_support import (
    BOTH_ACCEPT,
    CONVERT_ARGS,
    HttpServerTestCase,
    legacy_initialize,
    legacy_request,
    modern_call,
    modern_headers,
    modern_request,
)
from unit_expert_mcp.server import (
    INSPECT_LOCK,
    INSPECT_LOG,
    INSPECT_SOURCE_HEADER,
    encode_config_token,
    inspect_groups,
    inspect_groups_requested,
    inspect_query,
    inspect_snapshot,
)

# Fields the log had before the extra ones were added. They must stay.
ORIGINAL_FIELDS = {
    "seq",
    "time",
    "httpMethod",
    "path",
    "era",
    "method",
    "requestHeaders",
    "requestBody",
    "status",
    "responseBody",
    "rawResponse",
    "responseHeaders",
    "serverEra",
    "injected",
}
ADDED_FIELDS = {"timestamp", "durationMs", "query", "cfg", "configSource", "source"}
SESSION_ON = {"session": {"validate": True}}


def latest_seq() -> int:
    entries = inspect_snapshot()
    return entries[0]["seq"] if entries else 0


class InspectLogTestCase(HttpServerTestCase):
    def fetch(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Like request(), but reads the whole body (request() cuts a GET 200 short
        because the legacy GET /mcp stream never ends)."""
        if method == "GET" and path.startswith("/mcp"):
            return self.request(method, path, headers=headers, body=body)
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, raw
        finally:
            conn.close()

    def recorded(
        self,
        method: str,
        path: str = "/mcp",
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        expected: int = 1,
    ) -> tuple[int, dict[str, str], bytes, list[dict[str, Any]]]:
        """Send one request and return it with the entries it added (oldest first)."""
        before = latest_seq()
        status, response_headers, raw = self.fetch(method, path, headers, body)
        # A few branches record right after writing the response, so wait briefly.
        deadline = time.monotonic() + 2
        while True:
            entries = [entry for entry in inspect_snapshot() if entry["seq"] > before]
            if len(entries) >= expected or time.monotonic() > deadline:
                break
            time.sleep(0.01)
        time.sleep(0.02)  # a late duplicate would show up here
        entries = [entry for entry in inspect_snapshot() if entry["seq"] > before]
        return status, response_headers, raw, list(reversed(entries))

    def recorded_post(
        self,
        payload: Any,
        *,
        config: dict | None = None,
        headers: dict[str, str] | None = None,
        accept: str | None = BOTH_ACCEPT,
        path: str | None = None,
    ) -> tuple[int, dict[str, str], bytes, list[dict[str, Any]]]:
        request_headers = {"Content-Type": "application/json"}
        if accept is not None:
            request_headers["Accept"] = accept
        request_headers.update(headers or {})
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return self.recorded(
            "POST", path or self.path(config), headers=request_headers, body=body
        )

    def assert_one(
        self,
        result: tuple[int, dict[str, str], bytes, list[dict[str, Any]]],
        status: int,
        http_method: str,
    ) -> dict[str, Any]:
        """The request got ``status`` and the log gained exactly one matching entry."""
        self.assertEqual(result[0], status)
        self.assertEqual(len(result[3]), 1, result[3])
        entry = result[3][0]
        self.assertEqual((entry["httpMethod"], entry["status"], entry["path"]), (http_method, status, "/mcp"))
        return entry


class EntryFieldsTest(InspectLogTestCase):
    def test_buffer_keeps_200_entries(self) -> None:
        self.assertEqual(INSPECT_LOG.maxlen, 200)

    def test_entry_keeps_the_original_fields_and_adds_the_new_ones(self) -> None:
        entry = self.assert_one(self.recorded_post(legacy_request("tools/list")), 200, "POST")
        self.assertEqual(set(entry), ORIGINAL_FIELDS | ADDED_FIELDS)
        self.assertRegex(entry["time"], r"^\d\d:\d\d:\d\d$")
        self.assertEqual(entry["method"], "tools/list")
        self.assertEqual(entry["era"], "2025")

    def test_timestamp_is_iso_8601_utc_with_milliseconds(self) -> None:
        entry = self.recorded_post(legacy_request("tools/list"))[3][0]
        self.assertRegex(entry["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(entry["timestamp"][11:19], entry["time"])

    def test_duration_covers_an_injected_delay(self) -> None:
        config = {"server": {"delayEnabled": True, "delaySeconds": 0.2}}
        entry = self.recorded_post(legacy_request("tools/list"), config=config)[3][0]
        self.assertIsInstance(entry["durationMs"], float)
        self.assertGreaterEqual(entry["durationMs"], 200)
        self.assertLess(entry["durationMs"], 5000)

    def test_duration_covers_a_tool_call_delay(self) -> None:
        config = {"toolCall": {"delaySeconds": 0.2}}
        payload = legacy_request("tools/call", name="convert_length", arguments=CONVERT_ARGS)
        entry = self.recorded_post(payload, config=config)[3][0]
        self.assertGreaterEqual(entry["durationMs"], 200)

    def test_duration_of_a_plain_request_is_small_and_not_negative(self) -> None:
        entry = self.recorded_post(legacy_request("tools/list"))[3][0]
        self.assertGreaterEqual(entry["durationMs"], 0)
        self.assertLess(entry["durationMs"], 2000)

    def test_query_and_cfg_of_a_cfg_url(self) -> None:
        token = encode_config_token({"toolsList": {"mode": "empty"}})
        result = self.recorded_post(legacy_request("tools/list"), path=f"/mcp?cfg={token}&x=1")
        entry = self.assert_one(result, 200, "POST")
        self.assertEqual(entry["query"], f"cfg={token}&x=1")
        self.assertEqual(entry["cfg"], token)
        self.assertEqual(entry["configSource"], "url")

    def test_no_cfg_is_null_and_the_config_is_global(self) -> None:
        entry = self.recorded_post(legacy_request("tools/list"))[3][0]
        self.assertEqual(entry["query"], "")
        self.assertIsNone(entry["cfg"])
        self.assertEqual(entry["configSource"], "global")

    def test_an_empty_cfg_parameter_counts_as_no_cfg(self) -> None:
        entry = self.recorded_post(legacy_request("tools/list"), path="/mcp?cfg=")[3][0]
        self.assertEqual(entry["query"], "cfg=")
        self.assertIsNone(entry["cfg"])
        self.assertEqual(entry["configSource"], "global")

    def test_scenario_header_is_the_config_source(self) -> None:
        result = self.recorded_post(
            legacy_request("tools/list"), headers={"X-MCP-Test-Scenario": "tools-list-empty"}
        )
        entry = self.assert_one(result, 200, "POST")
        self.assertEqual(entry["configSource"], "header")
        self.assertIsNone(entry["cfg"])
        self.assertEqual(entry["responseBody"]["result"]["tools"], [])

    def test_cfg_url_wins_over_the_scenario_header(self) -> None:
        token = encode_config_token({"serverEra": "legacy"})
        entry = self.recorded_post(
            legacy_request("tools/list"),
            path=f"/mcp?cfg={token}",
            headers={"X-MCP-Test-Scenario": "tools-list-empty"},
        )[3][0]
        self.assertEqual(entry["configSource"], "url")
        self.assertEqual(len(entry["responseBody"]["result"]["tools"]) > 0, True)

    def test_source_header_is_recorded(self) -> None:
        entry = self.recorded_post(
            legacy_request("tools/list"), headers={INSPECT_SOURCE_HEADER: "scenario-page"}
        )[3][0]
        self.assertEqual(entry["source"], "scenario-page")
        # It is its own field, not one of the logged MCP wire headers.
        self.assertNotIn(INSPECT_SOURCE_HEADER.lower(), {name.lower() for name in entry["requestHeaders"]})

    def test_source_is_null_without_the_header(self) -> None:
        self.assertIsNone(self.recorded_post(legacy_request("tools/list"))[3][0]["source"])

    def test_authorization_header_is_recorded_masked(self) -> None:
        entry = self.recorded_post(
            legacy_request("tools/list"), headers={"Authorization": "Bearer not-a-real-token"}
        )[3][0]
        recorded = entry["requestHeaders"]["Authorization"]
        self.assertRegex(recorded, r"^Bearer \*\*\*oken \(len 16, fp [0-9a-f]{8}\)$")
        self.assertNotIn("not-a-real-token", json.dumps(entry))


class EveryBranchIsRecordedTest(InspectLogTestCase):
    """Each /mcp answer adds exactly one entry, including the early refusals."""

    def get(self, config: dict | None = None, **headers: str):
        return self.recorded("GET", self.path(config), headers=headers)

    # -- POST -------------------------------------------------------------------
    def test_normal_legacy_and_modern_exchanges(self) -> None:
        self.assert_one(self.recorded_post(legacy_initialize()), 200, "POST")
        entry = self.assert_one(
            self.recorded_post(modern_request("server/discover"), headers=modern_headers("server/discover")),
            200,
            "POST",
        )
        self.assertEqual(entry["era"], "2026")

    def test_custom_header_missing_is_401(self) -> None:
        result = self.recorded_post(legacy_request("tools/list"), config={"customHeader": {"enabled": True}})
        entry = self.assert_one(result, 401, "POST")
        self.assertEqual(result[2], b"Unauthorized")
        self.assertEqual(entry["responseBody"], {"error": "Unauthorized"})
        self.assertEqual(entry["injected"], ["customHeader.enabled"])
        self.assertEqual(entry["requestBody"]["method"], "tools/list")
        self.assertEqual(entry["serverEra"], "both")

    def test_custom_header_wrong_value_is_403(self) -> None:
        result = self.recorded_post(
            legacy_request("tools/list"),
            config={"customHeader": {"enabled": True}},
            headers={"X-Mock-Auth": "nope"},
        )
        entry = self.assert_one(result, 403, "POST")
        self.assertEqual(entry["responseBody"], {"error": "Forbidden"})
        self.assertEqual(entry["injected"], ["customHeader.enabled"])

    def test_custom_header_accepted_is_recorded_once_as_the_result(self) -> None:
        result = self.recorded_post(
            legacy_request("tools/list"),
            config={"customHeader": {"enabled": True}},
            headers={"X-Mock-Auth": "allow"},
        )
        self.assertEqual(self.assert_one(result, 200, "POST")["injected"], [])

    def test_server_http_status_401_and_403(self) -> None:
        for status, text in ((401, "Unauthorized"), (403, "Forbidden")):
            with self.subTest(status=status):
                result = self.recorded_post(
                    legacy_request("tools/list"), config={"server": {"httpStatus": status}}
                )
                entry = self.assert_one(result, status, "POST")
                self.assertEqual(result[2], text.encode())
                self.assertEqual(entry["responseBody"], {"error": text})
                self.assertEqual(entry["injected"], [f"server.httpStatus={status}"])

    def test_server_http_status_through_the_scenario_header(self) -> None:
        result = self.recorded_post(legacy_request("tools/list"), headers={"X-MCP-Test-Scenario": "auth-401"})
        entry = self.assert_one(result, 401, "POST")
        self.assertEqual(entry["configSource"], "header")

    def test_invalid_cfg_token_is_400(self) -> None:
        result = self.recorded_post(legacy_request("tools/list"), path="/mcp?cfg=!!!")
        entry = self.assert_one(result, 400, "POST")
        self.assertEqual(result[2], b"invalid config token")
        self.assertEqual(entry["responseBody"], {"error": "invalid config token"})
        self.assertEqual((entry["cfg"], entry["configSource"]), ("!!!", "url"))
        self.assertIsNone(entry["serverEra"])

    def test_invalid_header_cfg_token_is_400(self) -> None:
        result = self.recorded_post(legacy_request("tools/list"), headers={"X-MCP-Test-Scenario": "cfg:!!!"})
        entry = self.assert_one(result, 400, "POST")
        self.assertEqual(entry["configSource"], "header")

    def test_legacy_accept_error_is_400(self) -> None:
        result = self.recorded_post(legacy_request("tools/list"), accept="application/json")
        entry = self.assert_one(result, 400, "POST")
        text = "Invalid Accept headers. Expected TEXT_EVENT_STREAM and APPLICATION_JSON"
        self.assertEqual(result[2], text.encode())
        self.assertEqual(entry["responseBody"], {"error": text})

    def test_modern_accept_errors_are_400(self) -> None:
        for method in ("tools/list", "subscriptions/listen"):
            with self.subTest(method=method):
                result = self.recorded_post(
                    modern_request(method), headers=modern_headers(method), accept="text/html"
                )
                self.assert_one(result, 400, "POST")

    def test_body_parse_error_is_400(self) -> None:
        result = self.recorded_post(b"{not json")
        entry = self.assert_one(result, 400, "POST")
        self.assertEqual(entry["responseBody"]["error"]["code"], -32700)
        self.assertEqual(entry["requestBody"], "{not json")

    def test_batch_body_is_400(self) -> None:
        self.assert_one(self.recorded_post([legacy_request("tools/list")]), 400, "POST")

    def test_origin_refusal_is_403(self) -> None:
        config = {"allowedOrigins": ["https://ok.example"]}
        for method in ("POST", "GET", "DELETE"):
            with self.subTest(method=method):
                headers = {"Origin": "https://evil.example", "Accept": BOTH_ACCEPT}
                if method == "POST":
                    result = self.recorded_post(legacy_request("tools/list"), config=config, headers=headers)
                else:
                    result = self.recorded(method, self.path(config), headers=headers)
                self.assertEqual(self.assert_one(result, 403, method)["injected"], ["allowedOrigins"])

    def test_oauth_challenge_is_401(self) -> None:
        result = self.recorded_post(legacy_request("tools/list"), config={"auth": {"mode": "oauth"}})
        self.assertEqual(self.assert_one(result, 401, "POST")["responseBody"], {"error": "invalid_token"})

    def test_raw_response_rule(self) -> None:
        rule = {"method": "tools/list", "status": 418, "body": '{"teapot":true}'}
        result = self.recorded_post(legacy_request("tools/list"), config={"rawResponses": [rule]})
        self.assertIs(self.assert_one(result, 418, "POST")["rawResponse"], True)

    def test_dropped_connection(self) -> None:
        payload = legacy_request("tools/call", name="convert_length", arguments=CONVERT_ARGS)
        before = latest_seq()
        with self.assertRaises(Exception):
            self.post(payload, config={"toolCall": {"mode": "disconnect"}})
        entries = [entry for entry in inspect_snapshot() if entry["seq"] > before]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["responseBody"], {"disconnect": True})

    # -- GET --------------------------------------------------------------------
    def test_get_stream_is_recorded(self) -> None:
        result = self.get(Accept="text/event-stream", **{"Mcp-Session-Id": "any"})
        entry = self.assert_one(result, 200, "GET")
        self.assertEqual(entry["responseBody"], {"text": ": connected"})
        self.assertIsNone(entry["requestBody"])
        self.assertIsNone(entry["method"])

    def test_get_accept_error_is_400(self) -> None:
        entry = self.assert_one(self.get(Accept="application/json"), 400, "GET")
        self.assertEqual(
            entry["responseBody"], {"error": "Invalid Accept header. Expected TEXT_EVENT_STREAM"}
        )

    def test_get_without_a_session_is_400(self) -> None:
        entry = self.assert_one(self.get(Accept="text/event-stream"), 400, "GET")
        self.assertEqual(entry["responseBody"], {"error": "Session ID required in mcp-session-id header"})

    def test_get_session_error_is_recorded_once(self) -> None:
        entry = self.assert_one(self.get(SESSION_ON, Accept="text/event-stream"), 400, "GET")
        self.assertEqual(entry["injected"], ["session.validate"])

    def test_get_from_a_modern_client_is_405(self) -> None:
        result = self.get(Accept="text/event-stream", **{"Mcp-Method": "tools/list"})
        entry = self.assert_one(result, 405, "GET")
        self.assertEqual(result[1]["allow"], "POST, OPTIONS")
        self.assertEqual(entry["responseBody"], {"error": "Method Not Allowed"})
        self.assertEqual(entry["responseHeaders"], {"Allow": "POST, OPTIONS"})

    def test_get_on_a_modern_only_server_is_405(self) -> None:
        entry = self.assert_one(self.get({"serverEra": "modern"}, Accept="text/event-stream"), 405, "GET")
        self.assertEqual(entry["serverEra"], "modern")

    def test_get_with_an_invalid_cfg_token_is_400(self) -> None:
        result = self.recorded("GET", "/mcp?cfg=!!!", headers={"Accept": "text/event-stream"})
        self.assertEqual(self.assert_one(result, 400, "GET")["cfg"], "!!!")

    def test_get_pre_json_rpc_refusals(self) -> None:
        cases = (
            ({"customHeader": {"enabled": True}}, 401, ["customHeader.enabled"]),
            ({"server": {"httpStatus": 403}}, 403, ["server.httpStatus=403"]),
            ({"server": {"httpStatus": 401, "target": "initialize"}}, 401, ["server.httpStatus=401"]),
        )
        for config, status, injected in cases:
            with self.subTest(config=config):
                entry = self.assert_one(self.get(config, Accept="text/event-stream"), status, "GET")
                self.assertEqual(entry["injected"], injected)

    def test_get_oauth_challenge_is_401(self) -> None:
        result = self.get({"auth": {"mode": "oauth"}}, Accept="text/event-stream")
        entry = self.assert_one(result, 401, "GET")
        self.assertEqual(entry["responseBody"], {"error": "invalid_token"})

    # -- DELETE -----------------------------------------------------------------
    def test_delete_is_405_by_default(self) -> None:
        result = self.recorded("DELETE", "/mcp")
        entry = self.assert_one(result, 405, "DELETE")
        self.assertEqual(entry["responseHeaders"], {"Allow": result[1]["allow"]})

    def test_delete_with_an_invalid_cfg_token_is_405(self) -> None:
        self.assert_one(self.recorded("DELETE", "/mcp?cfg=!!!"), 405, "DELETE")

    def test_delete_session_error_is_recorded_once(self) -> None:
        result = self.recorded("DELETE", self.path(SESSION_ON))
        self.assertEqual(self.assert_one(result, 400, "DELETE")["injected"], ["session.validate"])

    def test_delete_pre_json_rpc_refusal(self) -> None:
        config = {**SESSION_ON, "server": {"httpStatus": 403}}
        entry = self.assert_one(self.recorded("DELETE", self.path(config)), 403, "DELETE")
        self.assertEqual(entry["injected"], ["server.httpStatus=403"])

    def test_delete_oauth_challenge_is_401(self) -> None:
        config = {**SESSION_ON, "auth": {"mode": "oauth"}}
        self.assert_one(self.recorded("DELETE", self.path(config)), 401, "DELETE")

    # -- other methods ----------------------------------------------------------
    def test_methods_without_a_handler_are_405(self) -> None:
        for method in ("PUT", "PATCH", "HEAD"):
            with self.subTest(method=method):
                result = self.recorded(method, "/mcp")
                entry = self.assert_one(result, 405, method)
                self.assertEqual(entry["responseHeaders"], {"Allow": result[1]["allow"]})
                self.assertEqual(result[1]["allow"], "GET, POST, OPTIONS")

    def test_preflight_is_recorded(self) -> None:
        result = self.recorded("OPTIONS", "/mcp", headers={"Origin": "https://page.example"})
        entry = self.assert_one(result, 204, "OPTIONS")
        self.assertIsNone(entry["responseBody"])

    def test_other_paths_are_not_recorded(self) -> None:
        for method, path in (
            ("GET", "/healthz"),
            ("GET", "/inspect/log"),
            ("GET", "/scenario/meta"),
            ("OPTIONS", "/scenario"),
            ("PUT", "/scenario"),
            ("GET", "/nope"),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.recorded(method, path, expected=0)[3], [])


class SourceHeaderTest(InspectLogTestCase):
    """X-Unit-Expert-Source is read for the log only."""

    def compare(self, payload: dict, headers: dict[str, str], config: dict | None = None) -> None:
        def strip(response_headers: dict[str, str]) -> dict[str, str]:
            return {name: value for name, value in response_headers.items() if name != "date"}

        plain = self.post(payload, headers=headers, config=config)
        tagged = self.post(payload, headers={**headers, INSPECT_SOURCE_HEADER: "scenario-page"}, config=config)
        self.assertEqual(tagged[0], plain[0])
        self.assertEqual(tagged[3], plain[3])
        self.assertEqual(strip(tagged[1]), strip(plain[1]))

    def test_modern_responses_do_not_change(self) -> None:
        self.compare(modern_request("server/discover"), modern_headers("server/discover"))
        self.compare(modern_request("tools/list"), modern_headers("tools/list"))
        self.compare(*modern_call("convert_length", CONVERT_ARGS))
        self.compare(*modern_call("convert_length", CONVERT_ARGS), config={"serverEra": "modern"})

    def test_modern_param_header_validation_ignores_it(self) -> None:
        arguments = {"region": "kr", "count": 3}
        payload, headers = modern_call(
            "echo_header_params",
            arguments,
            param_headers={"Mcp-Param-Region": "kr", "Mcp-Param-Count": "3"},
        )
        status, _, body, _ = self.post(payload, headers={**headers, INSPECT_SOURCE_HEADER: "scenario-page"})
        self.assertEqual(status, 200)
        self.assertNotIn("error", body)
        self.compare(payload, headers)

    def test_a_refused_modern_request_is_refused_the_same_way(self) -> None:
        payload, headers = modern_call("echo_header_params", {"region": "kr"})
        self.compare(payload, headers)  # missing Mcp-Param-Region: -32020 either way

    def test_legacy_responses_do_not_change(self) -> None:
        self.compare(legacy_request("tools/list"), {})
        self.compare(legacy_request("tools/call", name="convert_length", arguments=CONVERT_ARGS), {})

    def test_refusals_do_not_change(self) -> None:
        self.compare(legacy_request("tools/list"), {}, config={"server": {"httpStatus": 401}})
        self.compare(legacy_request("tools/list"), {}, config={"auth": {"mode": "oauth"}})


class InspectLogEndpointTest(InspectLogTestCase):
    def log(self, query: str = "") -> tuple[int, Any]:
        status, _, raw = self.fetch("GET", f"/inspect/log{query}")
        return status, json.loads(raw)

    def seqs(self, query: str) -> list[int]:
        status, body = self.log(query)
        self.assertEqual(status, 200)
        self.assertEqual(list(body), ["exchanges", "secrets"])
        return [entry["seq"] for entry in body["exchanges"]]

    def setUp(self) -> None:
        self.token = encode_config_token({"toolsList": {"mode": "empty"}})
        self.other = encode_config_token({"toolsList": {"mode": "null"}})
        self.start = latest_seq()
        self.with_token = [
            self.recorded_post(legacy_request("tools/list"), path=f"/mcp?cfg={self.token}")[3][0]["seq"]
            for _ in range(2)
        ]
        self.without = self.recorded_post(legacy_request("tools/list"))[3][0]["seq"]
        self.with_other = self.recorded_post(
            legacy_request("tools/list"), path=f"/mcp?cfg={self.other}"
        )[3][0]["seq"]

    def test_without_a_query_the_response_is_the_whole_log(self) -> None:
        status, body = self.log()
        self.assertEqual(status, 200)
        self.assertEqual(list(body), ["exchanges", "secrets"])
        self.assertEqual(body["secrets"], "masked")
        self.assertIsInstance(body["exchanges"], list)
        self.assertEqual([entry["seq"] for entry in body["exchanges"]], [e["seq"] for e in inspect_snapshot()])
        # Newest first, as before.
        self.assertEqual(body["exchanges"][0]["seq"], self.with_other)

    def test_since_returns_only_newer_entries(self) -> None:
        self.assertEqual(
            self.seqs(f"?since={self.start}"),
            [self.with_other, self.without, *reversed(self.with_token)],
        )
        self.assertEqual(self.seqs(f"?since={self.without}"), [self.with_other])
        self.assertEqual(self.seqs(f"?since={self.with_other}"), [])

    def test_cfg_returns_only_that_token(self) -> None:
        self.assertEqual(self.seqs(f"?since={self.start}&cfg={self.token}"), list(reversed(self.with_token)))
        self.assertEqual(self.seqs(f"?since={self.start}&cfg={self.other}"), [self.with_other])
        self.assertEqual(self.seqs("?cfg=never-used-token"), [])

    def test_empty_cfg_returns_requests_without_a_token(self) -> None:
        self.assertEqual(self.seqs(f"?since={self.start}&cfg="), [self.without])
        for entry in self.log("?cfg=")[1]["exchanges"]:
            self.assertIsNone(entry["cfg"])

    def test_limit_keeps_the_newest(self) -> None:
        self.assertEqual(self.seqs("?limit=2"), [self.with_other, self.without])
        self.assertEqual(self.seqs(f"?cfg={self.token}&limit=1"), [self.with_token[-1]])
        self.assertEqual(self.seqs("?limit=0"), [])
        self.assertEqual(len(self.seqs("?limit=100000")), len(inspect_snapshot()))

    def test_bad_numbers_are_400(self) -> None:
        for query in ("?since=abc", "?limit=-1", "?limit=1.5", "?since="):
            with self.subTest(query=query):
                status, body = self.log(query)
                self.assertEqual(status, 400)
                self.assertIs(body["ok"], False)
                self.assertRegex(body["error"], r"must be a non-negative integer")

    def test_unknown_parameters_are_ignored(self) -> None:
        self.assertEqual(self.seqs("?other=1"), [entry["seq"] for entry in inspect_snapshot()])

    def test_inspect_query_matches_the_endpoint(self) -> None:
        self.assertEqual(
            [entry["seq"] for entry in inspect_query(f"since={self.start}&cfg={self.token}")],
            list(reversed(self.with_token)),
        )
        with self.assertRaises(ValueError):
            inspect_query("since=x")

    def test_reading_the_log_does_not_add_to_it(self) -> None:
        before = latest_seq()
        self.log("?limit=1")
        self.log()
        self.assertEqual(latest_seq(), before)


class TimestampOrderTest(InspectLogTestCase):
    def test_seq_and_timestamp_grow_together(self) -> None:
        first = self.recorded_post(legacy_request("tools/list"))[3][0]
        second = self.recorded_post(legacy_request("tools/list"))[3][0]
        self.assertEqual(second["seq"], first["seq"] + 1)
        self.assertLessEqual(first["timestamp"], second["timestamp"])
        self.assertTrue(re.fullmatch(r".*Z", second["timestamp"]))


class AddressGroupsTest(InspectLogTestCase):
    """GET /inspect/log?groups=1 adds the log summed up per MCP address."""

    def setUp(self) -> None:
        super().setUp()
        with INSPECT_LOCK:
            INSPECT_LOG.clear()

    def log(self, query: str) -> dict[str, Any]:
        status, _, raw = self.fetch("GET", "/inspect/log" + query)
        self.assertEqual(status, 200)
        return json.loads(raw)

    def test_without_the_parameter_the_answer_is_unchanged(self) -> None:
        self.recorded_post(legacy_request("tools/list"))
        self.assertEqual(set(self.log("")), {"exchanges", "secrets"})
        self.assertEqual(set(self.log("?groups=0")), {"exchanges", "secrets"})
        self.assertEqual(inspect_groups_requested("since=3&groups=1"), True)
        self.assertEqual(inspect_groups_requested("since=3"), False)

    def test_one_group_per_address_newest_first(self) -> None:
        errors = {"toolCall": {"mode": "is-error"}}
        token = encode_config_token(errors)
        self.recorded_post(legacy_request("tools/list"))
        self.recorded_post(legacy_request("tools/list"), config=errors)
        self.recorded_post(legacy_request("tools/list"), config=errors, headers={INSPECT_SOURCE_HEADER: "inspect-console"})
        self.recorded("OPTIONS", "/mcp")
        body = self.log("?groups=1")
        groups = body["groups"]
        self.assertEqual([group["cfg"] for group in groups], [token, None])
        self.assertEqual([(group["count"], group["screen"]) for group in groups], [(2, 1), (1, 0)])
        newest = max(entry["seq"] for entry in body["exchanges"] if entry["cfg"] == token)
        self.assertEqual(groups[0]["lastSeq"], newest)
        by_seq = {entry["seq"]: entry for entry in body["exchanges"]}
        self.assertEqual(groups[0]["lastTimestamp"], by_seq[newest]["timestamp"])
        self.assertEqual(groups, inspect_groups())

    def test_groups_cover_the_whole_log_whatever_the_other_filters(self) -> None:
        errors = {"toolCall": {"mode": "is-error"}}
        self.recorded_post(legacy_request("tools/list"))
        self.recorded_post(legacy_request("tools/list"), config=errors)
        everything = self.log("?groups=1")["groups"]
        only = self.log(f"?groups=1&cfg={encode_config_token(errors)}&since=0")
        self.assertEqual(only["groups"], everything)
        self.assertEqual({entry["cfg"] for entry in only["exchanges"]}, {encode_config_token(errors)})
        plain = self.log("?groups=1&cfg=")
        self.assertEqual({entry["cfg"] for entry in plain["exchanges"]}, {None})
        self.assertEqual(plain["groups"], everything)
