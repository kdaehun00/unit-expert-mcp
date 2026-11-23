"""``toolCall.*`` injection, shared by the legacy and modern paths."""

from __future__ import annotations

import time
import unittest
from http.client import RemoteDisconnected

from mcp_test_support import (
    CONVERT_ARGS,
    HttpServerTestCase,
    legacy_request,
    modern_call,
    modern_headers,
    modern_request,
    parse_sse,
)
from unit_expert_mcp.server import TOOL_CALL_MODES, handle_json_rpc, inspect_snapshot, normalize_config

ERAS = ("legacy", "modern")


def tool_call(era: str, config: dict | None, name: str = "convert_length", request_id: object = 5, progress=None):
    """handle_json_rpc for a tools/call on the given client era."""
    if era == "modern":
        payload, headers = modern_call(name, CONVERT_ARGS, request_id)
        if progress is not None:
            payload["params"]["_meta"]["progressToken"] = progress
        return payload, headers
    payload = legacy_request("tools/call", request_id, name=name, arguments=CONVERT_ARGS)
    if progress is not None:
        payload["params"]["_meta"] = {"progressToken": progress}
    return payload, {}


def run(era: str, config: dict | None, **kwargs):
    payload, headers = tool_call(era, config, **kwargs)
    return handle_json_rpc(payload, config=config, headers=headers)


def inject(mode: str, **extra: object) -> dict:
    return {"toolCall": {"mode": mode, **extra}}


class ToolCallModeTest(unittest.TestCase):
    def test_normal(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                status, _, payload = run(era, None)
                self.assertEqual(status, 200)
                assert payload is not None
                self.assertEqual(payload["result"]["content"][0]["text"], "1 m = 100 cm")

    def test_is_error(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                status, _, payload = run(era, inject("is-error"))
                self.assertEqual(status, 200)
                assert payload is not None
                self.assertIs(payload["result"]["isError"], True)
                self.assertEqual(payload["id"], 5)
                # Only modern results carry resultType.
                self.assertEqual("resultType" in payload["result"], era == "modern")

    def test_json_rpc_error_with_configurable_code(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                status, _, payload = run(era, inject("json-rpc-error"))
                self.assertEqual(status, 200)
                assert payload is not None
                self.assertEqual(payload["error"]["code"], -32603)
                status, _, payload = run(era, inject("json-rpc-error", errorCode=-32000))
                assert payload is not None
                self.assertEqual(payload["error"]["code"], -32000)

    def test_http_status_with_configurable_code(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                status, _, payload = run(era, inject("http-status"))
                self.assertEqual(status, 500)
                self.assertIsNone(payload)
                for code in (401, 403, 404, 429, 502):
                    status, _, _ = run(era, inject("http-status", httpStatus=code))
                    self.assertEqual(status, code)

    def test_id_mismatch(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                status, _, payload = run(era, inject("id-mismatch"))
                self.assertEqual(status, 200)
                assert payload is not None
                self.assertNotEqual(payload["id"], 5)
                self.assertEqual(payload["result"]["content"][0]["text"], "1 m = 100 cm")
                _, _, payload = run(era, inject("id-mismatch"), request_id="abc")
                assert payload is not None
                self.assertNotEqual(payload["id"], "abc")

    def test_target_tools_limits_the_injection(self) -> None:
        config = inject("is-error", targetTools=["convert_weight"])
        for era in ERAS:
            with self.subTest(era=era):
                _, _, payload = run(era, config)
                assert payload is not None
                self.assertIs(payload["result"]["isError"], False)
                _, _, payload = run(era, config, name="convert_weight")
                assert payload is not None
                self.assertEqual(payload["result"]["content"][0]["text"], "Injected tool execution error")

    def test_empty_target_tools_means_every_tool(self) -> None:
        for era in ERAS:
            for name in ("convert_length", "list_supported_units"):
                with self.subTest(era=era, name=name):
                    _, _, payload = run(era, inject("is-error"), name=name)
                    assert payload is not None
                    self.assertIs(payload["result"]["isError"], True)

    def test_other_methods_are_not_affected(self) -> None:
        config = inject("http-status")
        status, _, payload = handle_json_rpc(legacy_request("tools/list"), config=config)
        self.assertEqual(status, 200)
        status, _, payload = handle_json_rpc(
            modern_request("tools/list"), config=config, headers=modern_headers("tools/list")
        )
        self.assertEqual(status, 200)

    def test_modern_protocol_errors_come_before_the_injection(self) -> None:
        payload, headers = modern_call("convert_length", CONVERT_ARGS)
        headers.pop("Mcp-Name")
        status, _, body = handle_json_rpc(payload, config=inject("is-error"), headers=headers)
        self.assertEqual(status, 400)
        assert body is not None
        self.assertEqual(body["error"]["code"], -32020)

    def test_modern_unknown_tool_and_param_mismatch_keep_their_protocol_error(self) -> None:
        payload, headers = modern_call("no_such_tool")
        status, _, body = handle_json_rpc(payload, config=inject("is-error"), headers=headers)
        assert body is not None
        self.assertEqual(body["error"]["code"], -32602)

        payload, headers = modern_call("echo_header_params", {"region": "us-west1"})
        status, _, body = handle_json_rpc(payload, config=inject("http-status"), headers=headers)
        self.assertEqual(status, 400)
        assert body is not None
        self.assertEqual(body["error"]["code"], -32020)

    def test_legacy_unknown_tool_is_a_result_and_gets_the_injection(self) -> None:
        status, _, body = run("legacy", inject("json-rpc-error"), name="no_such_tool")
        self.assertEqual(status, 200)
        assert body is not None
        self.assertEqual(body["error"]["code"], -32603)

    def test_input_required_result_can_be_wrapped(self) -> None:
        payload, headers = modern_call("confirm_action", {"action": "x"}, capabilities={"elicitation": {}})
        status, _, body = handle_json_rpc(payload, config=inject("id-mismatch"), headers=headers)
        self.assertEqual(status, 200)
        assert body is not None
        self.assertNotEqual(body["id"], 1)
        self.assertEqual(body["result"]["resultType"], "input_required")

    def test_lenient_modern_path_is_covered_too(self) -> None:
        payload, headers = modern_call("convert_length", CONVERT_ARGS)
        config = {"strict2026": False, **inject("json-rpc-error")}
        status, _, body = handle_json_rpc(payload, config=config, headers=headers)
        self.assertEqual(status, 200)
        assert body is not None
        self.assertEqual(body["error"]["code"], -32603)

    def test_config_validation(self) -> None:
        for bad in (
            {"toolCall": {"mode": "explode"}},
            {"toolCall": {"httpStatus": 200}},
            {"toolCall": {"httpStatus": "500"}},
            {"toolCall": {"delaySeconds": -0.1}},
            {"toolCall": {"delaySeconds": 121}},
            {"toolCall": {"targetTools": "convert_length"}},
            {"toolCall": {"targetTools": [1]}},
            {"toolCall": {"sseNotificationCount": 21}},
            {"toolCall": {"errorCode": 1.5}},
            {"toolCall": []},
        ):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    normalize_config(bad)

    def test_every_mode_is_accepted(self) -> None:
        for mode in TOOL_CALL_MODES:
            self.assertEqual(normalize_config(inject(mode))["toolCall"]["mode"], mode)


class ToolCallHttpTest(HttpServerTestCase):
    def call(self, era: str, config: dict | None, **kwargs):
        timeout = kwargs.pop("timeout", 10)
        payload, headers = tool_call(era, config, **kwargs)
        return self.post(payload, config=config, headers=headers, timeout=timeout)

    def test_sse_sends_notifications_then_the_final_response(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                status, headers, _, raw = self.call(era, inject("sse", sseNotificationCount=3))
                self.assertEqual(status, 200)
                self.assertIn("text/event-stream", headers["content-type"])
                messages = parse_sse(raw)
                self.assertEqual(len(messages), 4)
                for notification in messages[:3]:
                    self.assertNotIn("id", notification)
                    self.assertEqual(notification["method"], "notifications/message")
                final = messages[-1]
                self.assertEqual(final["id"], 5)
                self.assertEqual(final["result"]["content"][0]["text"], "1 m = 100 cm")
                self.assertEqual("resultType" in final["result"], era == "modern")

    def test_sse_uses_progress_notifications_when_a_token_is_given(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                _, _, _, raw = self.call(era, inject("sse"), progress="tok-1")
                messages = parse_sse(raw)
                self.assertEqual(len(messages), 3)
                self.assertEqual(
                    [message["method"] for message in messages[:2]],
                    ["notifications/progress", "notifications/progress"],
                )
                self.assertEqual(messages[0]["params"]["progressToken"], "tok-1")
                self.assertEqual([m["params"]["progress"] for m in messages[:2]], [1, 2])

    def test_sse_with_zero_notifications_is_just_the_response(self) -> None:
        _, _, _, raw = self.call("modern", inject("sse", sseNotificationCount=0))
        messages = parse_sse(raw)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], 5)

    def test_http_status_on_the_wire(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                status, _, payload, raw = self.call(era, inject("http-status", httpStatus=502))
                self.assertEqual(status, 502)
                self.assertIsNone(payload)
                self.assertEqual(raw, b"Injected HTTP 502")

    def test_empty_response(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                status, headers, _, raw = self.call(era, inject("empty"))
                self.assertEqual(status, 200)
                self.assertEqual(raw, b"")
                self.assertEqual(headers["content-length"], "0")
                self.assertIn("application/json", headers["content-type"])

    def test_disconnect(self) -> None:
        for era in ERAS:
            with self.subTest(era=era):
                with self.assertRaises((RemoteDisconnected, ConnectionError)):
                    self.call(era, inject("disconnect"))

    def test_delay_applies_with_any_mode(self) -> None:
        for mode in ("normal", "is-error"):
            with self.subTest(mode=mode):
                started = time.monotonic()
                status, _, payload, _ = self.call("legacy", inject(mode, delaySeconds=0.3))
                self.assertGreaterEqual(time.monotonic() - started, 0.3)
                self.assertEqual(status, 200)
                self.assertEqual(payload["result"]["isError"], mode == "is-error")

    def test_delay_skips_untargeted_tools(self) -> None:
        started = time.monotonic()
        config = inject("normal", delaySeconds=5, targetTools=["convert_weight"])
        status, _, _, _ = self.call("modern", config)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(status, 200)

    def test_delay_outlasts_a_client_timeout(self) -> None:
        with self.assertRaises(TimeoutError):
            self.call("modern", inject("normal", delaySeconds=2), timeout=0.3)

    def test_server_keeps_serving_after_a_disconnect(self) -> None:
        with self.assertRaises((RemoteDisconnected, ConnectionError)):
            self.call("legacy", inject("disconnect"))
        status, _, payload, _ = self.call("legacy", None)
        self.assertEqual(status, 200)
        self.assertIn("result", payload)

    def test_inspect_log_names_the_injection(self) -> None:
        self.call("modern", inject("sse", delaySeconds=0.01, targetTools=["convert_length"]))
        entry = inspect_snapshot()[0]
        self.assertEqual(entry["injected"], ["toolCall.mode=sse", "toolCall.delaySeconds=0.01"])
        self.assertEqual(len(entry["responseBody"]["events"]), 3)
        with self.assertRaises((RemoteDisconnected, ConnectionError)):
            self.call("legacy", inject("disconnect"))
        entry = inspect_snapshot()[0]
        self.assertEqual(entry["injected"], ["toolCall.mode=disconnect"])
        self.assertEqual(entry["responseBody"], {"disconnect": True})

    def test_via_header_scenario(self) -> None:
        payload, headers = tool_call("legacy", None)
        status, _, body, _ = self.post(payload, headers={"X-MCP-Test-Scenario": "tool-call-is-error"})
        self.assertEqual(status, 200)
        self.assertIs(body["result"]["isError"], True)


if __name__ == "__main__":
    unittest.main()
