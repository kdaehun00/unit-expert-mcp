"""Two-round input_required (MRTR) flow with the ``convert_interactive`` test tool.

Call 1 -> asks for the target unit. Call 2 (inputResponses.target_unit) -> asks
for the number of decimals. Call 3 (inputResponses.precision) -> complete. Each
retry carries only the latest requestState and that round's inputResponses.
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
    modern_headers,
    modern_request,
)
from unit_expert_mcp import server as server_module
from unit_expert_mcp.server import (
    INTERACTIVE_TOOL,
    MODERN_TEST_TOOL_NAMES,
    MRTR_TOOL,
    handle_json_rpc,
    scenario_meta,
)

ELICITATION = {"elicitation": {}}
ARGS = {"value": 3, "from_unit": "km"}
LENGTH_TARGETS = ["mm", "cm", "m", "in", "ft", "yd", "mi"]


def call(
    request_id: int = 1,
    *,
    capabilities: dict | None = ELICITATION,
    arguments: dict | None = None,
    config: dict | None = None,
    **params,
):
    payload, headers = modern_call(
        INTERACTIVE_TOOL,
        ARGS if arguments is None else arguments,
        request_id,
        capabilities=capabilities,
        **params,
    )
    return handle_json_rpc(payload, config=config, headers=headers)


def result_of(reply) -> dict:
    status, _, payload = reply
    assert status == 200 and payload is not None, reply
    return payload["result"]


def unit(to_unit: str = "m") -> dict:
    return {"target_unit": {"action": "accept", "content": {"to_unit": to_unit}}}


def precision(decimals: object = 2) -> dict:
    return {"precision": {"action": "accept", "content": {"decimals": decimals}}}


def first_round(**kwargs) -> dict:
    return result_of(call(1, **kwargs))


def second_round(to_unit: str = "m", **kwargs) -> dict:
    state = first_round(**kwargs)["requestState"]
    return result_of(call(2, inputResponses=unit(to_unit), requestState=state, **kwargs))


def text_of(result: dict) -> str:
    return result["content"][0]["text"]


def message_of(result: dict, key: str) -> str:
    return result["inputRequests"][key]["params"]["message"]


class ConvertInteractiveTest(unittest.TestCase):
    def assert_round(self, result: dict, key: str) -> dict:
        self.assertEqual(result["resultType"], "input_required")
        self.assertNotIn("content", result)
        self.assertEqual(list(result["inputRequests"]), [key])
        request = result["inputRequests"][key]
        self.assertEqual(request["method"], "elicitation/create")
        self.assertEqual(request["params"]["mode"], "form")
        self.assertIsInstance(result["requestState"], str)
        self.assertTrue(result["requestState"])
        return request["params"]

    def test_first_call_asks_for_a_unit_of_the_same_category(self) -> None:
        params = self.assert_round(first_round(), "target_unit")
        self.assertEqual(params["message"], "Convert 3 km to which unit?")
        schema = params["requestedSchema"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["to_unit"])
        self.assertEqual(schema["properties"]["to_unit"]["type"], "string")
        self.assertEqual(schema["properties"]["to_unit"]["enum"], LENGTH_TARGETS)

    def test_every_category_offers_its_other_units(self) -> None:
        for from_unit, category in (
            ("kg", "weight"),
            ("celsius", "temperature"),
            ("m2", "area"),
            ("liters", "volume"),
        ):
            with self.subTest(from_unit=from_unit):
                result = first_round(arguments={"value": 1, "from_unit": from_unit})
                offered = result["inputRequests"]["target_unit"]["params"]["requestedSchema"][
                    "properties"
                ]["to_unit"]["enum"]
                units = server_module.SUPPORTED_UNITS[category]
                self.assertEqual(len(offered), len(units) - 1)
                self.assertLess(set(offered), set(units))

    def test_second_call_asks_for_decimals(self) -> None:
        params = self.assert_round(second_round(), "precision")
        self.assertEqual(params["message"], "Convert 3 km to m with how many decimal places?")
        self.assertEqual(params["requestedSchema"]["required"], ["decimals"])
        decimals = params["requestedSchema"]["properties"]["decimals"]
        self.assertEqual(
            {key: decimals[key] for key in ("type", "minimum", "maximum")},
            {"type": "integer", "minimum": 0, "maximum": 10},
        )

    def test_three_calls_complete_with_only_the_latest_state(self) -> None:
        first = first_round()
        second = result_of(call(2, inputResponses=unit(), requestState=first["requestState"]))
        self.assertNotEqual(second["requestState"], first["requestState"])
        # Round 3 sends neither the round-1 answer nor the round-1 state.
        status, _, payload = call(3, inputResponses=precision(), requestState=second["requestState"])
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["id"], 3)
        result = payload["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIs(result["isError"], False)
        self.assertEqual(text_of(result), "3 km = 3000.00 m")
        self.assertNotIn("inputRequests", result)
        self.assertNotIn("requestState", result)

    def test_decimals_and_units_shape_the_text(self) -> None:
        cases = (
            ({"value": 3, "from_unit": "km"}, "m", 0, "3 km = 3000 m"),
            ({"value": 1, "from_unit": "in"}, "cm", 10, "1 in = 2.5400000000 cm"),
            ({"value": 100, "from_unit": "c"}, "f", 1, "100 c = 212.0 f"),
            ({"value": 2.5, "from_unit": "Kilograms"}, "g", 2.0, "2.5 kg = 2500.00 g"),
        )
        for arguments, to_unit, decimals, expected in cases:
            with self.subTest(expected=expected):
                state = second_round(to_unit, arguments=arguments)["requestState"]
                result = result_of(
                    call(3, arguments=arguments, inputResponses=precision(decimals), requestState=state)
                )
                self.assertEqual(text_of(result), expected)

    def test_decline_and_cancel_complete_in_either_round(self) -> None:
        for key, state in (
            ("target_unit", first_round()["requestState"]),
            ("precision", second_round()["requestState"]),
        ):
            for action, word in (("decline", "declined"), ("cancel", "cancelled")):
                with self.subTest(key=key, action=action):
                    result = result_of(
                        call(5, inputResponses={key: {"action": action}}, requestState=state)
                    )
                    self.assertEqual(result["resultType"], "complete")
                    self.assertIs(result["isError"], False)
                    self.assertEqual(
                        text_of(result), f"Conversion of 3 km was {word}; nothing was converted."
                    )

    def test_bad_round_one_answers_ask_for_the_unit_again(self) -> None:
        state = first_round()["requestState"]
        cases = (
            None,
            {},
            {"target_unit": {"action": "accept"}},
            {"target_unit": {"action": "accept", "content": {}}},
            {"target_unit": {"action": "accept", "content": {"to_unit": "kg"}}},
            {"target_unit": {"action": "accept", "content": {"to_unit": "km"}}},
            {"target_unit": {"action": "accept", "content": {"to_unit": 5}}},
            {"target_unit": {"action": "maybe"}},
            {"target_unit": "m"},
            precision(),
        )
        for responses in cases:
            with self.subTest(responses=responses):
                params = {"requestState": state}
                if responses is not None:
                    params["inputResponses"] = responses
                result = result_of(call(2, **params))
                self.assert_round(result, "target_unit")
                message = message_of(result, "target_unit")
                self.assertTrue(message.startswith("Convert 3 km to which unit?\n"), message)
                self.assertEqual(len(message.splitlines()), 2)
                # The re-issued state is a round-1 state again.
                again = result_of(call(3, inputResponses=unit(), requestState=result["requestState"]))
                self.assertIn("precision", again["inputRequests"])

    def test_bad_round_two_answers_ask_for_decimals_again(self) -> None:
        state = second_round()["requestState"]
        cases = (
            None,
            {},
            {"precision": {"action": "accept"}},
            precision(-1),
            precision(11),
            precision(2.5),
            precision("2"),
            precision(True),
            unit("cm"),
        )
        for responses in cases:
            with self.subTest(responses=responses):
                params = {"requestState": state}
                if responses is not None:
                    params["inputResponses"] = responses
                result = result_of(call(3, **params))
                self.assert_round(result, "precision")
                message = message_of(result, "precision")
                self.assertTrue(
                    message.startswith("Convert 3 km to m with how many decimal places?\n"), message
                )
                # The unit chosen in round 1 survives the re-ask.
                done = result_of(call(4, inputResponses=precision(1), requestState=result["requestState"]))
                self.assertEqual(text_of(done), "3 km = 3000.0 m")

    def test_reasons_name_what_was_wrong(self) -> None:
        state = first_round()["requestState"]
        result = result_of(call(2, inputResponses=unit("kg"), requestState=state))
        self.assertIn("to_unit must be one of: mm, cm, m,", message_of(result, "target_unit"))
        state = second_round()["requestState"]
        result = result_of(call(3, inputResponses=precision(11), requestState=state))
        self.assertIn("decimals must be an integer from 0 to 10.", message_of(result, "precision"))

    def test_round_one_state_with_a_round_two_answer_stays_in_round_one(self) -> None:
        first = first_round()
        result_of(call(2, inputResponses=unit(), requestState=first["requestState"]))
        # The client kept the old state: the server has no unit yet, so it asks for it.
        result = result_of(call(3, inputResponses=precision(), requestState=first["requestState"]))
        self.assert_round(result, "target_unit")
        self.assertIn("inputResponses.target_unit", message_of(result, "target_unit"))

    def test_both_answers_with_the_round_one_state_advance_one_round_only(self) -> None:
        state = first_round()["requestState"]
        result = result_of(call(2, inputResponses={**unit(), **precision()}, requestState=state))
        self.assert_round(result, "precision")

    def test_responses_without_state_start_over(self) -> None:
        self.assert_round(result_of(call(2, inputResponses=unit())), "target_unit")
        result = result_of(call(2, inputResponses=precision()))
        self.assert_round(result, "target_unit")
        self.assertEqual(message_of(result, "target_unit"), "Convert 3 km to which unit?")

    def test_tampered_state_is_rejected(self) -> None:
        state = second_round()["requestState"]
        body, signature = state.split(".")
        for bad in (state[:-2] + "xx", "garbage", f"{body}x.{signature}", "", 5):
            with self.subTest(state=bad):
                status, _, payload = call(3, inputResponses=precision(), requestState=bad)
                self.assertEqual(status, 200)
                assert payload is not None
                self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)

    def test_state_is_bound_to_the_original_arguments(self) -> None:
        for other in ({"value": 4, "from_unit": "km"}, {"value": 3, "from_unit": "m"}):
            for state, responses in (
                (first_round()["requestState"], unit("cm")),
                (second_round()["requestState"], precision()),
            ):
                with self.subTest(other=other, responses=responses):
                    _, _, payload = call(2, arguments=other, inputResponses=responses, requestState=state)
                    assert payload is not None
                    self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)

    def test_state_of_the_other_mrtr_tool_is_rejected(self) -> None:
        state = server_module.issue_request_state(MRTR_TOOL, ARGS)
        _, _, payload = call(2, inputResponses=unit(), requestState=state)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)

    def test_expired_state_is_rejected(self) -> None:
        for state, responses in (
            (first_round()["requestState"], unit()),
            (second_round()["requestState"], precision()),
        ):
            later = server_module.time.time() + server_module.MRTR_STATE_TTL_SECONDS + 5
            with mock.patch.object(server_module.time, "time", return_value=later):
                _, _, payload = call(2, inputResponses=responses, requestState=state)
            assert payload is not None
            self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)

    def test_invalid_params_status_follows_the_config(self) -> None:
        status, _, payload = call(
            2, requestState="garbage", config={"modern": {"invalidParamsHttpStatus": 400}}
        )
        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)

    def test_client_without_elicitation_gets_missing_capability(self) -> None:
        state = first_round()["requestState"]
        for capabilities in ({}, {"sampling": {}}, {"elicitation": True}):
            for params in ({}, {"inputResponses": unit(), "requestState": state}):
                with self.subTest(capabilities=capabilities, retry=bool(params)):
                    status, _, payload = call(1, capabilities=capabilities, **params)
                    self.assertEqual(status, 400)
                    assert payload is not None
                    self.assertEqual(payload["error"]["code"], ERR_MISSING_CAPABILITY)
                    self.assertEqual(
                        payload["error"]["data"], {"requiredCapabilities": {"elicitation": {}}}
                    )
                    self.assertIn(INTERACTIVE_TOOL, payload["error"]["message"])
                    self.assertNotIn("result", payload)

    def test_unknown_unit_and_bad_value_are_tool_errors(self) -> None:
        cases = (
            ({"value": 3, "from_unit": "parsec"}, "unsupported unit 'parsec'. Supported units: mm, cm,"),
            ({"value": 3, "from_unit": ""}, "unit must be a non-empty string."),
            ({"value": 3}, "unit must be a non-empty string."),
            ({"value": "abc", "from_unit": "km"}, "value must be a finite number."),
            ({"from_unit": "km"}, "value must be a finite number."),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                result = first_round(arguments=arguments)
                self.assertEqual(result["resultType"], "complete")
                self.assertIs(result["isError"], True)
                self.assertTrue(text_of(result).startswith(expected), text_of(result))
                self.assertNotIn("inputRequests", result)

    def test_listed_on_the_modern_path_only_and_hidden_with_test_tools(self) -> None:
        _, _, modern = handle_json_rpc(modern_request("tools/list"), headers=modern_headers("tools/list"))
        _, _, hidden = handle_json_rpc(
            modern_request("tools/list"),
            config={"modern": {"testTools": False}},
            headers=modern_headers("tools/list"),
        )
        _, _, legacy = handle_json_rpc(legacy_request("tools/list"))
        assert modern is not None and hidden is not None and legacy is not None
        tool = next(tool for tool in modern["result"]["tools"] if tool["name"] == INTERACTIVE_TOOL)
        self.assertEqual(tool["inputSchema"]["required"], ["value", "from_unit"])
        self.assertNotIn("to_unit", tool["inputSchema"]["properties"])
        self.assertIs(tool["annotations"]["readOnlyHint"], True)
        for phrase in ("has not said which unit", "target unit", "decimal places"):
            self.assertIn(phrase, tool["description"])
        for listing in (hidden, legacy):
            self.assertNotIn(INTERACTIVE_TOOL, [tool["name"] for tool in listing["result"]["tools"]])

    def test_hidden_tool_is_unknown_and_names_the_option(self) -> None:
        payload, headers = modern_call(INTERACTIVE_TOOL, ARGS, capabilities=ELICITATION)
        config = {"modern": {"testTools": False}}
        _, _, body = handle_json_rpc(payload, config=config, headers=headers)
        assert body is not None
        self.assertEqual(body["error"]["code"], ERR_INVALID_PARAMS)
        reply = server_module.handle_mcp_exchange(payload, config=config, headers=headers)
        self.assertEqual(reply["injected"], ["modern.testTools=false"])

    def test_legacy_path_does_not_know_the_tool(self) -> None:
        _, _, payload = handle_json_rpc(legacy_request("tools/call", name=INTERACTIVE_TOOL, arguments=ARGS))
        assert payload is not None
        self.assertIs(payload["result"]["isError"], True)
        self.assertNotIn("resultType", payload["result"])

    def test_service_name_shows_in_the_description_only(self) -> None:
        config = {"mcp": {"serviceName": "Acme Units"}}
        _, _, listing = handle_json_rpc(
            modern_request("tools/list"), config=config, headers=modern_headers("tools/list")
        )
        assert listing is not None
        tool = next(tool for tool in listing["result"]["tools"] if tool["name"] == INTERACTIVE_TOOL)
        self.assertTrue(tool["description"].startswith("Acme Units "))
        self.assertEqual(message_of(first_round(config=config), "target_unit"), "Convert 3 km to which unit?")

    def test_tool_call_injection_targets_the_tool(self) -> None:
        config = {"toolCall": {"mode": "json-rpc-error", "targetTools": [INTERACTIVE_TOOL]}}
        _, _, payload = call(1, config=config)
        assert payload is not None
        self.assertIn("error", payload)
        other, headers = modern_call(MRTR_TOOL, {"action": "x"}, capabilities=ELICITATION)
        _, _, untouched = handle_json_rpc(other, config=config, headers=headers)
        assert untouched is not None
        self.assertEqual(untouched["result"]["resultType"], "input_required")

    def test_scenario_meta_and_send_page_name_the_tool(self) -> None:
        self.assertEqual(MODERN_TEST_TOOL_NAMES, ("echo_header_params", "confirm_action", INTERACTIVE_TOOL))
        option = next(
            option for option in scenario_meta()["options"] if option["path"] == "modern.testTools"
        )
        self.assertIn("echo_header_params, confirm_action, convert_interactive", option["description"])
        page = server_module.render_send_page()
        self.assertIn('<option value="convert_interactive"></option>', page)
        self.assertIn('"convert_interactive": {"value": 3, "from_unit": "km"}', page)


class ConfirmActionUnchangedTest(unittest.TestCase):
    """The shared helpers must leave the one-round tool byte-for-byte as it was."""

    def test_first_round_result_is_what_it_was(self) -> None:
        payload, headers = modern_call(MRTR_TOOL, {"action": "delete-report"}, capabilities=ELICITATION)
        with mock.patch.object(server_module.time, "time", return_value=1_800_000_000):
            _, _, body = handle_json_rpc(payload, headers=headers)
            state = server_module.issue_request_state(MRTR_TOOL, {"action": "delete-report"})
        assert body is not None
        result = dict(body["result"])
        result.pop("_meta", None)
        self.assertEqual(
            result,
            {
                "resultType": "input_required",
                "inputRequests": {
                    "confirm": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Confirm the action 'delete-report'?",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {
                                    "confirmed": {
                                        "type": "boolean",
                                        "description": "true to run the action",
                                    }
                                },
                                "required": ["confirmed"],
                            },
                        },
                    }
                },
                "requestState": state,
            },
        )
        # Same token layout as before: tool, args digest and exp only.
        decoded = server_module.json.loads(server_module._b64url_decode(state.split(".")[0]))
        self.assertEqual(sorted(decoded), ["args", "exp", "tool"])

    def test_state_of_the_interactive_tool_is_rejected(self) -> None:
        arguments = {"action": "delete-report"}
        state = server_module.issue_request_state(INTERACTIVE_TOOL, arguments, {"to_unit": "m"})
        payload, headers = modern_call(
            MRTR_TOOL, arguments, capabilities=ELICITATION, requestState=state,
            inputResponses={"confirm": {"action": "accept", "content": {"confirmed": True}}},
        )
        _, _, body = handle_json_rpc(payload, headers=headers)
        assert body is not None
        self.assertEqual(body["error"]["code"], ERR_INVALID_PARAMS)


class ConvertInteractiveHttpTest(HttpServerTestCase):
    def round(self, request_id: str, **params) -> tuple[int, dict]:
        payload, headers = modern_call(INTERACTIVE_TOOL, ARGS, request_id, capabilities=ELICITATION, **params)
        status, _, body, _ = self.post(payload, headers=headers)
        return status, body

    def test_three_round_trips_over_http(self) -> None:
        status, body = self.round("call-1")
        self.assertEqual(status, 200)
        self.assertEqual(list(body["result"]["inputRequests"]), ["target_unit"])
        status, body = self.round(
            "call-2", inputResponses=unit(), requestState=body["result"]["requestState"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(list(body["result"]["inputRequests"]), ["precision"])
        status, body = self.round(
            "call-3", inputResponses=precision(), requestState=body["result"]["requestState"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], "call-3")
        self.assertEqual(body["result"]["resultType"], "complete")
        self.assertEqual(body["result"]["content"][0]["text"], "3 km = 3000.00 m")

    def test_missing_capability_over_http(self) -> None:
        payload, headers = modern_call(INTERACTIVE_TOOL, ARGS)
        status, _, body, _ = self.post(payload, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], ERR_MISSING_CAPABILITY)

    def test_mixed_oauth_protects_the_tool_when_listed(self) -> None:
        config = {"auth": {"mode": "mixed-oauth", "protectedTools": [INTERACTIVE_TOOL]}}
        payload, headers = modern_call(INTERACTIVE_TOOL, ARGS, capabilities=ELICITATION)
        status, _, _, _ = self.post(payload, config=config, headers=headers)
        self.assertEqual(status, 401)
        payload, headers = modern_call(MRTR_TOOL, {"action": "x"}, capabilities=ELICITATION)
        status, _, _, _ = self.post(payload, config=config, headers=headers)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
