"""input_required (MRTR) round trip with the ``confirm_action`` test tool.

Spec: basic/patterns/mrtr. First call -> InputRequiredResult (elicitation/create
+ requestState). Retry with inputResponses + the same requestState and a new
JSON-RPC id -> complete.
"""

from __future__ import annotations

import unittest
from unittest import mock

from mcp_test_support import (
    ERR_INVALID_PARAMS,
    ERR_MISSING_CAPABILITY,
    HttpServerTestCase,
    legacy_request,
    modern_call,
)
from unit_expert_mcp import server as server_module
from unit_expert_mcp.server import MRTR_TOOL, handle_json_rpc

ELICITATION = {"elicitation": {}}
ARGS = {"action": "delete-report"}


def call(request_id: int = 1, *, capabilities: dict | None = ELICITATION, arguments: dict | None = None, **params):
    payload, headers = modern_call(
        MRTR_TOOL, ARGS if arguments is None else arguments, request_id, capabilities=capabilities, **params
    )
    return handle_json_rpc(payload, headers=headers)


def first_round() -> dict:
    status, _, payload = call(1)
    assert status == 200 and payload is not None
    return payload["result"]


def accept(confirmed: bool = True) -> dict:
    return {"confirm": {"action": "accept", "content": {"confirmed": confirmed}}}


class InputRequiredTest(unittest.TestCase):
    def test_first_call_returns_input_required(self) -> None:
        result = first_round()
        self.assertEqual(result["resultType"], "input_required")
        self.assertNotIn("content", result)
        self.assertEqual(list(result["inputRequests"]), ["confirm"])
        request = result["inputRequests"]["confirm"]
        self.assertEqual(request["method"], "elicitation/create")
        self.assertEqual(request["params"]["mode"], "form")
        self.assertIn("delete-report", request["params"]["message"])
        self.assertEqual(
            request["params"]["requestedSchema"]["properties"]["confirmed"]["type"], "boolean"
        )
        self.assertIsInstance(result["requestState"], str)
        self.assertTrue(result["requestState"])

    def test_retry_with_responses_and_state_completes(self) -> None:
        state = first_round()["requestState"]
        status, _, payload = call(2, inputResponses=accept(), requestState=state)
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["id"], 2)
        result = payload["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIs(result["isError"], False)
        self.assertEqual(result["content"][0]["text"], "Action 'delete-report' confirmed.")
        self.assertNotIn("inputRequests", result)

    def test_accept_with_confirmed_false_completes_without_running(self) -> None:
        state = first_round()["requestState"]
        _, _, payload = call(2, inputResponses=accept(False), requestState=state)
        assert payload is not None
        self.assertEqual(payload["result"]["content"][0]["text"], "Action 'delete-report' was not confirmed.")

    def test_decline_and_cancel_complete(self) -> None:
        for action, word in (("decline", "declined"), ("cancel", "cancelled")):
            with self.subTest(action=action):
                state = first_round()["requestState"]
                _, _, payload = call(2, inputResponses={"confirm": {"action": action}}, requestState=state)
                assert payload is not None
                self.assertEqual(payload["result"]["resultType"], "complete")
                self.assertIn(word, payload["result"]["content"][0]["text"])

    def test_incomplete_responses_ask_again_instead_of_failing(self) -> None:
        state = first_round()["requestState"]
        cases = (
            None,
            {},
            {"other": {"action": "accept"}},
            {"confirm": {"action": "accept"}},
            {"confirm": {"action": "accept", "content": {"confirmed": "yes"}}},
        )
        for responses in cases:
            with self.subTest(responses=responses):
                params = {"requestState": state}
                if responses is not None:
                    params["inputResponses"] = responses
                status, _, payload = call(2, **params)
                self.assertEqual(status, 200)
                assert payload is not None
                self.assertEqual(payload["result"]["resultType"], "input_required")
                self.assertIn("confirm", payload["result"]["inputRequests"])

    def test_responses_without_state_start_over(self) -> None:
        status, _, payload = call(2, inputResponses=accept())
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["result"]["resultType"], "input_required")

    def test_unknown_extra_responses_are_ignored(self) -> None:
        state = first_round()["requestState"]
        responses = {**accept(), "unexpected": {"action": "accept"}}
        _, _, payload = call(2, inputResponses=responses, requestState=state)
        assert payload is not None
        self.assertEqual(payload["result"]["resultType"], "complete")

    def test_tampered_state_is_rejected(self) -> None:
        state = first_round()["requestState"]
        body, signature = state.split(".")
        for bad in (state[:-2] + "xx", "garbage", f"{body}x.{signature}", "", 5):
            with self.subTest(state=bad):
                status, _, payload = call(2, inputResponses=accept(), requestState=bad)
                self.assertEqual(status, 200)
                assert payload is not None
                self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)

    def test_state_is_bound_to_the_original_arguments(self) -> None:
        state = first_round()["requestState"]
        _, _, payload = call(2, arguments={"action": "drop-database"}, inputResponses=accept(), requestState=state)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)

    def test_expired_state_is_rejected(self) -> None:
        state = first_round()["requestState"]
        later = server_module.time.time() + server_module.MRTR_STATE_TTL_SECONDS + 5
        with mock.patch.object(server_module.time, "time", return_value=later):
            _, _, payload = call(2, inputResponses=accept(), requestState=state)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)

    def test_client_without_elicitation_gets_missing_capability(self) -> None:
        for capabilities in ({}, {"sampling": {}}, {"elicitation": True}):
            with self.subTest(capabilities=capabilities):
                status, _, payload = call(1, capabilities=capabilities)
                self.assertEqual(status, 400)
                assert payload is not None
                self.assertEqual(payload["error"]["code"], ERR_MISSING_CAPABILITY)
                self.assertEqual(payload["error"]["data"], {"requiredCapabilities": {"elicitation": {}}})
                self.assertNotIn("result", payload)

    def test_invalid_argument_is_a_tool_execution_error(self) -> None:
        status, _, payload = call(1, arguments={"action": ""})
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIs(payload["result"]["isError"], True)
        self.assertEqual(payload["result"]["resultType"], "complete")

    def test_legacy_path_does_not_know_the_tool(self) -> None:
        _, _, payload = handle_json_rpc(legacy_request("tools/call", name=MRTR_TOOL, arguments=ARGS))
        assert payload is not None
        self.assertIs(payload["result"]["isError"], True)
        self.assertNotIn("resultType", payload["result"])


class InputRequiredHttpTest(HttpServerTestCase):
    def test_round_trip_over_http(self) -> None:
        payload, headers = modern_call(MRTR_TOOL, ARGS, "call-1", capabilities=ELICITATION)
        status, response_headers, body, _ = self.post(payload, headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("application/json", response_headers["content-type"])
        self.assertEqual(body["result"]["resultType"], "input_required")

        retry, headers = modern_call(
            MRTR_TOOL,
            ARGS,
            "call-2",
            capabilities=ELICITATION,
            inputResponses=accept(),
            requestState=body["result"]["requestState"],
        )
        status, _, body, _ = self.post(retry, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], "call-2")
        self.assertEqual(body["result"]["resultType"], "complete")

    def test_missing_capability_over_http(self) -> None:
        payload, headers = modern_call(MRTR_TOOL, ARGS)
        status, _, body, _ = self.post(payload, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], ERR_MISSING_CAPABILITY)


if __name__ == "__main__":
    unittest.main()
