"""2026-07-28 path follows the published spec by default (config ``strict2026``).

Rules checked here come from docs/SPEC_2026-07-28_NOTES.md ("테스트 서버 검증
체크리스트"). ``strict2026: false`` must bring the earlier lenient behaviour back.
"""

from __future__ import annotations

import base64
import json
import unittest

from mcp_test_support import (
    CONVERT_ARGS,
    ERR_HEADER_MISMATCH,
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_MISSING_CAPABILITY,
    ERR_UNSUPPORTED_VERSION,
    META_CAPS,
    META_SERVER,
    META_VERSION,
    HttpServerTestCase,
    legacy_initialize,
    legacy_request,
    modern_call,
    modern_headers,
    modern_request,
    sentinel,
)
from unit_expert_mcp.server import (
    PROTOCOL_VERSION_2026,
    SERVER_VERSION,
    handle_json_rpc,
    issue_access_token,
    normalize_config,
)

LENIENT = {"strict2026": False}


def error_of(result: tuple) -> tuple[int, int]:
    status, _, payload = result
    assert payload is not None and "error" in payload, payload
    return status, payload["error"]["code"]


class RequiredHeaderTest(unittest.TestCase):
    def test_strict_is_the_default(self) -> None:
        self.assertIs(normalize_config({})["strict2026"], True)

    def test_missing_protocol_version_header_is_header_mismatch(self) -> None:
        result = handle_json_rpc(modern_request("tools/list"), headers={"Mcp-Method": "tools/list"})
        self.assertEqual(error_of(result), (400, ERR_HEADER_MISMATCH))

    def test_missing_method_header_is_header_mismatch(self) -> None:
        # Routed to the modern path by its _meta envelope alone.
        result = handle_json_rpc(
            modern_request("tools/list"), headers={"MCP-Protocol-Version": PROTOCOL_VERSION_2026}
        )
        self.assertEqual(error_of(result), (400, ERR_HEADER_MISMATCH))

    def test_missing_name_header_is_header_mismatch(self) -> None:
        payload, _ = modern_call("convert_length", CONVERT_ARGS)
        result = handle_json_rpc(payload, headers=modern_headers("tools/call"))
        self.assertEqual(error_of(result), (400, ERR_HEADER_MISMATCH))

    def test_header_names_are_case_insensitive(self) -> None:
        status, _, payload = handle_json_rpc(
            modern_request("tools/list"),
            headers={"mcp-protocol-version": PROTOCOL_VERSION_2026, "MCP-METHOD": "tools/list"},
        )
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIn("result", payload)

    def test_method_header_value_is_case_sensitive(self) -> None:
        result = handle_json_rpc(modern_request("tools/list"), headers=modern_headers("Tools/List"))
        self.assertEqual(error_of(result), (400, ERR_HEADER_MISMATCH))

    def test_lenient_mode_lets_a_missing_version_header_through(self) -> None:
        status, _, payload = handle_json_rpc(
            modern_request("tools/list"), config=LENIENT, headers={"Mcp-Method": "tools/list"}
        )
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(len(payload["result"]["tools"]), 6)


class EnvelopeTest(unittest.TestCase):
    def test_missing_meta_fields_are_invalid_params_400(self) -> None:
        for missing in (META_VERSION, META_CAPS):
            with self.subTest(missing=missing):
                payload = modern_request("tools/list")
                payload["params"]["_meta"].pop(missing)
                result = handle_json_rpc(payload, headers=modern_headers("tools/list"))
                self.assertEqual(error_of(result), (400, ERR_INVALID_PARAMS))

    def test_client_capabilities_must_be_an_object(self) -> None:
        payload = modern_request("tools/list")
        payload["params"]["_meta"][META_CAPS] = "none"
        result = handle_json_rpc(payload, headers=modern_headers("tools/list"))
        self.assertEqual(error_of(result), (400, ERR_INVALID_PARAMS))

    def test_version_header_must_match_meta(self) -> None:
        result = handle_json_rpc(
            modern_request("tools/list"), headers=modern_headers("tools/list", version="2026-01-01")
        )
        self.assertEqual(error_of(result), (400, ERR_HEADER_MISMATCH))

    def test_unsupported_version_carries_supported_and_requested(self) -> None:
        status, _, payload = handle_json_rpc(
            modern_request("tools/list", version="2099-01-01"),
            headers=modern_headers("tools/list", version="2099-01-01"),
        )
        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_UNSUPPORTED_VERSION)
        self.assertEqual(
            payload["error"]["data"],
            {"supported": [PROTOCOL_VERSION_2026], "requested": "2099-01-01"},
        )

    def test_legacy_version_header_with_modern_meta_is_header_mismatch(self) -> None:
        # _meta protocolVersion marks a modern request; a legacy version in the
        # header then disagrees with the body.
        payload = modern_request("tools/list")
        result = handle_json_rpc(payload, headers=modern_headers("tools/list", version="2025-11-25"))
        self.assertEqual(error_of(result), (400, ERR_HEADER_MISMATCH))
        # Lenient mode keeps routing it to the legacy path.
        status, _, body = handle_json_rpc(
            payload, config=LENIENT, headers=modern_headers("tools/list", version="2025-11-25")
        )
        self.assertEqual(status, 200)
        assert body is not None
        self.assertNotIn("resultType", body["result"])

    def test_legacy_request_with_gateway_routing_headers_stays_legacy(self) -> None:
        # No protocolVersion in _meta: Mcp-Method is only a routing hint here.
        status, _, body = handle_json_rpc(
            legacy_request("tools/list", _meta={"progressToken": 1}),
            headers={"Mcp-Method": "tools/list", "MCP-Protocol-Version": "2025-06-18"},
        )
        self.assertEqual(status, 200)
        assert body is not None
        self.assertNotIn("resultType", body["result"])

    def test_modern_notification_is_accepted_without_a_session(self) -> None:
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"_meta": {META_VERSION: PROTOCOL_VERSION_2026, META_CAPS: {}}},
        }
        status, _, body = handle_json_rpc(
            notification,
            config={"session": {"validate": True}},
            headers=modern_headers("notifications/cancelled"),
        )
        self.assertEqual(status, 202)
        self.assertIsNone(body)

    def test_modern_path_supports_only_2026(self) -> None:
        payload = modern_request("tools/list", version="2025-11-25")
        # No version header -> stays on the modern path (via Mcp-Method) and fails
        # there on the missing header, never succeeding with a legacy version.
        result = handle_json_rpc(payload, headers={"Mcp-Method": "tools/list"})
        self.assertEqual(error_of(result), (400, ERR_HEADER_MISMATCH))

    def test_null_and_non_scalar_ids_are_invalid_requests(self) -> None:
        for bad_id in (None, 1.5, True, [1], {"a": 1}):
            with self.subTest(id=bad_id):
                payload = modern_request("tools/list", request_id=bad_id)
                status, _, body = handle_json_rpc(payload, headers=modern_headers("tools/list"))
                self.assertEqual(status, 400)
                assert body is not None
                self.assertEqual(body["error"]["code"], ERR_INVALID_REQUEST)
                self.assertIsNone(body["id"])

    def test_string_and_integer_ids_are_accepted(self) -> None:
        for good_id in ("discover-1", 0, 7):
            with self.subTest(id=good_id):
                status, _, body = handle_json_rpc(
                    modern_request("tools/list", request_id=good_id),
                    headers=modern_headers("tools/list"),
                )
                self.assertEqual(status, 200)
                assert body is not None
                self.assertEqual(body["id"], good_id)


class McpNameTest(unittest.TestCase):
    def call(self, header_value: str, body_name: str = "convert_length"):
        payload, headers = modern_call(body_name, CONVERT_ARGS)
        headers["Mcp-Name"] = header_value
        return handle_json_rpc(payload, headers=headers)

    def test_plain_name_matches(self) -> None:
        status, _, payload = self.call("convert_length")
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["result"]["content"][0]["text"], "1 m = 100 cm")

    def test_base64_sentinel_is_decoded_before_comparison(self) -> None:
        status, _, payload = self.call(sentinel("convert_length"))
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["result"]["resultType"], "complete")

    def test_sentinel_without_padding_is_rejected(self) -> None:
        # Same strictness as the official Python SDK 2.3.0: padding is required.
        encoded = base64.b64encode(b"convert_length").decode("ascii")
        self.assertTrue(encoded.endswith("="))
        result = self.call("=?base64?" + encoded.rstrip("=") + "?=")
        self.assertEqual(error_of(result), (400, ERR_HEADER_MISMATCH))

    def test_surrounding_whitespace_is_not_part_of_the_value(self) -> None:
        for value in ("convert_length ", "convert_length\t", "  convert_length \t "):
            with self.subTest(value=value):
                status, _, _ = self.call(value)
                self.assertEqual(status, 200)
        status, _, _ = self.call(sentinel("convert_length") + " ")
        self.assertEqual(status, 200)
        # Whitespace inside the value still counts.
        self.assertEqual(error_of(self.call("convert length")), (400, ERR_HEADER_MISMATCH))

    def test_mcp_method_surrounding_whitespace_is_ignored(self) -> None:
        payload, headers = modern_call("convert_length", CONVERT_ARGS)
        headers["Mcp-Method"] = " tools/call\t "
        status, _, _ = handle_json_rpc(payload, headers=headers)
        self.assertEqual(status, 200)

    def test_decoded_name_mismatch_is_header_mismatch(self) -> None:
        self.assertEqual(error_of(self.call(sentinel("convert_weight"))), (400, ERR_HEADER_MISMATCH))

    def test_malformed_sentinel_is_header_mismatch(self) -> None:
        self.assertEqual(error_of(self.call("=?base64?!!!not-base64!!!?=")), (400, ERR_HEADER_MISMATCH))

    def test_sentinel_markers_are_case_sensitive(self) -> None:
        # "=?BASE64?" is not the sentinel, so the value is compared literally.
        value = sentinel("convert_length").replace("base64", "BASE64")
        self.assertEqual(error_of(self.call(value)), (400, ERR_HEADER_MISMATCH))

    def test_non_ascii_tool_name_travels_as_a_sentinel(self) -> None:
        # The unknown-tool answer proves the header check passed on the decoded name.
        status, _, payload = self.call(sentinel("단위_변환"), body_name="단위_변환")
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)
        self.assertEqual(payload["error"]["message"], "Unknown tool: 단위_변환")
        self.assertEqual(status, 200)

    def test_lenient_mode_compares_the_raw_header(self) -> None:
        payload, headers = modern_call("convert_length", CONVERT_ARGS)
        headers["Mcp-Name"] = sentinel("convert_length")
        result = handle_json_rpc(payload, config=LENIENT, headers=headers)
        self.assertEqual(error_of(result), (400, ERR_HEADER_MISMATCH))


class MethodAndToolTest(unittest.TestCase):
    def test_unknown_and_removed_methods_are_404_method_not_found(self) -> None:
        for method in ("initialize", "ping", "logging/setLevel", "resources/subscribe", "nope/nope"):
            with self.subTest(method=method):
                result = handle_json_rpc(modern_request(method), headers=modern_headers(method))
                self.assertEqual(error_of(result), (404, ERR_METHOD_NOT_FOUND))

    def test_unimplemented_name_bearing_method_needs_its_header_first(self) -> None:
        payload = modern_request("prompts/get", name="greeting")
        self.assertEqual(
            error_of(handle_json_rpc(payload, headers=modern_headers("prompts/get"))),
            (400, ERR_HEADER_MISMATCH),
        )
        headers = modern_headers("prompts/get", **{"Mcp-Name": "greeting"})
        self.assertEqual(error_of(handle_json_rpc(payload, headers=headers)), (404, ERR_METHOD_NOT_FOUND))

    def test_lenient_mode_answers_ping_and_unknown_methods_with_200(self) -> None:
        status, _, payload = handle_json_rpc(
            modern_request("ping"), config=LENIENT, headers=modern_headers("ping")
        )
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIn("result", payload)
        result = handle_json_rpc(
            modern_request("nope/nope"), config=LENIENT, headers=modern_headers("nope/nope")
        )
        self.assertEqual(error_of(result), (200, ERR_METHOD_NOT_FOUND))

    def test_unknown_tool_is_a_protocol_error(self) -> None:
        payload, headers = modern_call("no_such_tool")
        status, _, body = handle_json_rpc(payload, headers=headers)
        self.assertEqual(status, 200)
        assert body is not None
        self.assertEqual(body["error"], {"code": ERR_INVALID_PARAMS, "message": "Unknown tool: no_such_tool"})

    def test_unknown_tool_http_status_is_configurable(self) -> None:
        payload, headers = modern_call("no_such_tool")
        result = handle_json_rpc(
            payload, config={"modern": {"invalidParamsHttpStatus": 400}}, headers=headers
        )
        self.assertEqual(error_of(result), (400, ERR_INVALID_PARAMS))

    def test_tool_execution_failure_stays_an_is_error_result(self) -> None:
        payload, headers = modern_call("convert_length", {"value": 1, "from_unit": "m", "to_unit": "zz"})
        status, _, body = handle_json_rpc(payload, headers=headers)
        self.assertEqual(status, 200)
        assert body is not None
        self.assertIs(body["result"]["isError"], True)
        self.assertEqual(body["result"]["resultType"], "complete")

    def test_lenient_mode_reports_unknown_tool_as_is_error(self) -> None:
        payload, headers = modern_call("no_such_tool")
        status, _, body = handle_json_rpc(payload, config=LENIENT, headers=headers)
        self.assertEqual(status, 200)
        assert body is not None
        self.assertIs(body["result"]["isError"], True)

    def test_legacy_unknown_tool_is_unchanged(self) -> None:
        status, _, body = handle_json_rpc(legacy_request("tools/call", name="no_such_tool"))
        self.assertEqual(status, 200)
        assert body is not None
        self.assertIs(body["result"]["isError"], True)


class DiscoverAndListShapeTest(unittest.TestCase):
    def test_discover_result_shape(self) -> None:
        status, headers, payload = handle_json_rpc(
            modern_request("server/discover", "discover-1"),
            config={"mcp": {"identifier": "myServer"}, "discover": {"cacheTtlMs": 3600000, "cacheScope": "public"}},
            headers=modern_headers("server/discover"),
        )
        self.assertEqual(status, 200)
        self.assertNotIn("Mcp-Session-Id", headers)
        assert payload is not None
        result = payload["result"]
        self.assertEqual(
            result,
            {
                "resultType": "complete",
                "supportedVersions": [PROTOCOL_VERSION_2026],
                "capabilities": {"tools": {"listChanged": True}},
                "ttlMs": 3600000,
                "cacheScope": "public",
                "_meta": {META_SERVER: {"name": "myServer", "version": SERVER_VERSION}},
            },
        )

    def test_discover_defaults_include_cache_fields(self) -> None:
        _, _, payload = handle_json_rpc(
            modern_request("server/discover"), headers=modern_headers("server/discover")
        )
        assert payload is not None
        self.assertEqual(payload["result"]["ttlMs"], 0)
        self.assertEqual(payload["result"]["cacheScope"], "private")
        self.assertNotIn("serverInfo", payload["result"])

    def test_lenient_discover_keeps_the_old_shape(self) -> None:
        _, _, payload = handle_json_rpc(
            modern_request("server/discover"), config=LENIENT, headers=modern_headers("server/discover")
        )
        assert payload is not None
        self.assertIn("serverInfo", payload["result"])
        self.assertNotIn("ttlMs", payload["result"])

    def test_tools_list_follows_the_same_rules(self) -> None:
        _, _, payload = handle_json_rpc(
            modern_request("tools/list"),
            config={"mcp": {"identifier": "myServer"}},
            headers=modern_headers("tools/list"),
        )
        assert payload is not None
        result = payload["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["ttlMs"], 0)
        self.assertEqual(result["cacheScope"], "private")
        self.assertEqual(result["_meta"][META_SERVER]["name"], "myServer")
        self.assertNotIn("nextCursor", result)

    def test_test_tools_are_modern_only_and_can_be_hidden(self) -> None:
        _, _, modern = handle_json_rpc(modern_request("tools/list"), headers=modern_headers("tools/list"))
        _, _, hidden = handle_json_rpc(
            modern_request("tools/list"),
            config={"modern": {"testTools": False}},
            headers=modern_headers("tools/list"),
        )
        _, _, legacy = handle_json_rpc(legacy_request("tools/list"))
        assert modern is not None and hidden is not None and legacy is not None
        names = [tool["name"] for tool in modern["result"]["tools"]]
        self.assertIn("echo_header_params", names)
        self.assertIn("confirm_action", names)
        self.assertIn("convert_interactive", names)
        legacy_names = [tool["name"] for tool in legacy["result"]["tools"]]
        self.assertEqual([tool["name"] for tool in hidden["result"]["tools"]], legacy_names)
        self.assertEqual(len(legacy_names), 6)
        self.assertNotIn("x-mcp-header", json.dumps(legacy["result"]))


class RequiredCapabilityTest(unittest.TestCase):
    CONFIG = {"modern": {"requiredClientCapabilities": ["sampling", "roots"]}}

    def test_missing_capability_is_32021_with_required_capabilities(self) -> None:
        status, _, payload = handle_json_rpc(
            modern_request("tools/list", capabilities={"roots": {}}),
            config=self.CONFIG,
            headers=modern_headers("tools/list"),
        )
        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_MISSING_CAPABILITY)
        self.assertEqual(payload["error"]["data"], {"requiredCapabilities": {"sampling": {}}})

    def test_declared_capabilities_pass(self) -> None:
        status, _, _ = handle_json_rpc(
            modern_request("tools/list", capabilities={"roots": {}, "sampling": {}}),
            config=self.CONFIG,
            headers=modern_headers("tools/list"),
        )
        self.assertEqual(status, 200)

    def test_discover_is_exempt(self) -> None:
        status, _, _ = handle_json_rpc(
            modern_request("server/discover"), config=self.CONFIG, headers=modern_headers("server/discover")
        )
        self.assertEqual(status, 200)

    def test_unknown_capability_name_is_rejected_by_config(self) -> None:
        with self.assertRaises(ValueError):
            normalize_config({"modern": {"requiredClientCapabilities": ["telepathy"]}})


class ModernOnlyServerTest(unittest.TestCase):
    CONFIG = {"serverEra": "modern"}

    def test_legacy_initialize_names_the_supported_versions(self) -> None:
        status, headers, payload = handle_json_rpc(legacy_initialize(version="2025-06-18"), config=self.CONFIG)
        self.assertEqual(status, 400)
        self.assertNotIn("Mcp-Session-Id", headers)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_UNSUPPORTED_VERSION)
        self.assertEqual(
            payload["error"]["data"],
            {"supported": [PROTOCOL_VERSION_2026], "requested": "2025-06-18"},
        )

    def test_legacy_request_reports_its_header_version(self) -> None:
        _, _, payload = handle_json_rpc(
            legacy_request("tools/list"), config=self.CONFIG, headers={"MCP-Protocol-Version": "2025-11-25"}
        )
        assert payload is not None
        self.assertEqual(payload["error"]["data"]["requested"], "2025-11-25")

    def test_lenient_mode_has_no_error_data(self) -> None:
        _, _, payload = handle_json_rpc(legacy_initialize(), config={**self.CONFIG, **LENIENT})
        assert payload is not None
        self.assertNotIn("data", payload["error"])


class StrictHttpTest(HttpServerTestCase):
    def test_modern_only_get_and_delete_are_405_without_markers(self) -> None:
        for method in ("GET", "DELETE"):
            with self.subTest(method=method):
                status, headers, _ = self.request(
                    method,
                    self.path({"serverEra": "modern"}),
                    headers={"Accept": "text/event-stream", "Mcp-Session-Id": "abc"},
                )
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], "POST, OPTIONS")

    def test_lenient_modern_only_get_keeps_the_legacy_stream(self) -> None:
        status, _, _ = self.request(
            "GET",
            self.path({"serverEra": "modern", "strict2026": False}),
            headers={"Accept": "text/event-stream", "Mcp-Session-Id": "abc"},
        )
        self.assertEqual(status, 200)

    def test_default_server_keeps_the_legacy_get_stream(self) -> None:
        status, _, _ = self.request(
            "GET", "/mcp", headers={"Accept": "text/event-stream", "Mcp-Session-Id": "abc"}
        )
        self.assertEqual(status, 200)

    def test_modern_requests_never_receive_or_need_a_session_id(self) -> None:
        for config in (None, {"serverEra": "modern"}, {"session": {"validate": True}}):
            with self.subTest(config=config):
                headers = modern_headers("tools/list", **{"Mcp-Session-Id": "never-issued"})
                status, response_headers, payload, _ = self.post(
                    modern_request("tools/list"), config=config, headers=headers
                )
                self.assertEqual(status, 200)
                self.assertNotIn("mcp-session-id", response_headers)
                self.assertEqual(payload["result"]["resultType"], "complete")

    def test_batch_body_gets_a_json_rpc_error_instead_of_a_dropped_connection(self) -> None:
        batch = [modern_request("tools/list", 1), modern_request("tools/list", 2)]
        for config in (None, {"serverEra": "legacy"}, {"serverEra": "modern"}):
            with self.subTest(config=config):
                status, _, payload, _ = self.post(batch, config=config, headers=modern_headers("tools/list"))
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], ERR_INVALID_REQUEST)
                self.assertIsNone(payload["id"])
                self.assertIn("batch", payload["error"]["message"])

    def test_scalar_body_gets_a_json_rpc_error(self) -> None:
        status, _, payload, _ = self.post("hello")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], ERR_INVALID_REQUEST)

    def test_error_statuses_on_the_wire(self) -> None:
        status, _, payload, _ = self.post(modern_request("ping"), headers=modern_headers("ping"))
        self.assertEqual((status, payload["error"]["code"]), (404, ERR_METHOD_NOT_FOUND))
        status, _, payload, _ = self.post(
            modern_request("tools/list"), headers={"Mcp-Method": "tools/list"}
        )
        self.assertEqual((status, payload["error"]["code"]), (400, ERR_HEADER_MISMATCH))

    def test_subscriptions_listen_uses_the_strict_validation(self) -> None:
        payload = modern_request("subscriptions/listen", notifications={"toolsListChanged": True})
        status, _, body, _ = self.post(payload, headers={"Mcp-Method": "subscriptions/listen"})
        self.assertEqual((status, body["error"]["code"]), (400, ERR_HEADER_MISMATCH))
        status, headers, _, raw = self.post(payload, headers=modern_headers("subscriptions/listen"))
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        self.assertEqual(headers["x-accel-buffering"], "no")
        self.assertIn(b"notifications/subscriptions/acknowledged", raw)

    def test_json_only_accept_is_still_served(self) -> None:
        # Sending both media types is a client MUST; the spec does not tell the
        # server to refuse a client that lists only application/json.
        status, _, payload, _ = self.modern_post("tools/list", accept="application/json")
        self.assertEqual(status, 200)
        self.assertIn("result", payload)


class OriginTest(HttpServerTestCase):
    CONFIG = {"allowedOrigins": ["https://allowed.example"]}

    def test_default_accepts_any_origin(self) -> None:
        status, _, _, _ = self.modern_post("tools/list")
        self.assertEqual(status, 200)
        status, headers, _, _ = self.post(
            modern_request("tools/list"),
            headers={**modern_headers("tools/list"), "Origin": "https://anything.example"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], "https://anything.example")

    def test_listed_origin_and_missing_origin_pass(self) -> None:
        for origin in ("https://allowed.example", None):
            with self.subTest(origin=origin):
                headers = modern_headers("tools/list")
                if origin:
                    headers["Origin"] = origin
                status, _, _, _ = self.post(modern_request("tools/list"), config=self.CONFIG, headers=headers)
                self.assertEqual(status, 200)

    def test_unlisted_origin_is_403_on_every_method(self) -> None:
        headers = {**modern_headers("tools/list"), "Origin": "https://evil.example"}
        status, _, payload, _ = self.post(modern_request("tools/list"), config=self.CONFIG, headers=headers)
        self.assertEqual(status, 403)
        self.assertNotIn("id", payload)
        self.assertIn("error", payload)
        # Legacy requests are covered by the same allow-list.
        status, _, _, _ = self.post(
            legacy_initialize(), config=self.CONFIG, headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        for method in ("GET", "DELETE"):
            status, _, _ = self.request(
                method,
                self.path(self.CONFIG),
                headers={"Origin": "https://evil.example", "Accept": "text/event-stream"},
            )
            self.assertEqual(status, 403)

    def test_allowed_origins_must_be_a_list_of_strings(self) -> None:
        for bad in ("https://a.example", [1], [""]):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    normalize_config({"allowedOrigins": bad})


class AuthChallengeTest(HttpServerTestCase):
    CONFIG = {"auth": {"mode": "oauth"}}

    def test_modern_401_challenge_includes_scope(self) -> None:
        status, headers, _, _ = self.modern_post("tools/list", config=self.CONFIG)
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata="', headers["www-authenticate"])
        self.assertIn('scope="mcp"', headers["www-authenticate"])

    def test_legacy_401_challenge_is_unchanged(self) -> None:
        status, headers, _, _ = self.post(legacy_initialize(), config=self.CONFIG)
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata="', headers["www-authenticate"])
        self.assertNotIn("scope=", headers["www-authenticate"])

    def test_lenient_modern_401_challenge_is_unchanged(self) -> None:
        status, headers, _, _ = self.modern_post("tools/list", config={**self.CONFIG, "strict2026": False})
        self.assertEqual(status, 401)
        self.assertNotIn("scope=", headers["www-authenticate"])

    def bearer(self, scope: str) -> dict[str, str]:
        token = issue_access_token({"sub": "demo-user", "scope": scope, "typ": "access"})
        return {"Authorization": f"Bearer {token}"}

    def test_scope_is_not_checked_by_default(self) -> None:
        headers = {**modern_headers("tools/list"), **self.bearer("other")}
        status, _, _, _ = self.post(modern_request("tools/list"), config=self.CONFIG, headers=headers)
        self.assertEqual(status, 200)

    def test_enforce_scope_answers_403_insufficient_scope(self) -> None:
        config = {"auth": {"mode": "oauth", "enforceScope": True}}
        for payload, base in (
            (modern_request("tools/list"), modern_headers("tools/list")),
            (legacy_request("tools/list"), {}),
        ):
            with self.subTest(modern=bool(base)):
                status, headers, body, _ = self.post(
                    payload, config=config, headers={**base, **self.bearer("other")}
                )
                self.assertEqual(status, 403)
                challenge = headers["www-authenticate"]
                self.assertIn('error="insufficient_scope"', challenge)
                self.assertIn('scope="mcp"', challenge)
                self.assertIn('resource_metadata="', challenge)
                self.assertEqual(body["error"], "insufficient_scope")
                status, _, _, _ = self.post(
                    payload, config=config, headers={**base, **self.bearer("openid mcp")}
                )
                self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
