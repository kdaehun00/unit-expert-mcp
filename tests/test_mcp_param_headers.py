"""``Mcp-Param-*`` validation for tools whose inputSchema uses ``x-mcp-header``.

Spec: basic/transports/streamable-http "Custom Headers from Tool Parameters".
The test tool ``echo_header_params`` mirrors region/greeting (string), count
(integer) and verbose (boolean) into headers; ``note`` is not mirrored.
"""

from __future__ import annotations

import json
import unittest

from mcp_test_support import (
    ERR_HEADER_MISMATCH,
    HttpServerTestCase,
    legacy_request,
    modern_call,
    modern_headers,
    modern_request,
    sentinel,
)
from unit_expert_mcp.server import (
    HEADER_PARAMS_TOOL,
    decode_mcp_header_value,
    handle_json_rpc,
    header_param_properties,
    modern_test_tools,
)


def call(arguments: dict, param_headers: dict[str, str] | None = None, config: dict | None = None):
    payload, headers = modern_call(HEADER_PARAMS_TOOL, arguments, param_headers=param_headers)
    return handle_json_rpc(payload, config=config, headers=headers)


class ToolDefinitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = next(tool for tool in modern_test_tools() if tool["name"] == HEADER_PARAMS_TOOL)
        self.properties = self.tool["inputSchema"]["properties"]

    def test_annotated_properties_cover_string_integer_boolean(self) -> None:
        annotated = {
            name: (schema["type"], schema["x-mcp-header"])
            for name, schema in self.properties.items()
            if "x-mcp-header" in schema
        }
        self.assertEqual(
            annotated,
            {
                "region": ("string", "Region"),
                "greeting": ("string", "Greeting"),
                "count": ("integer", "Count"),
                "verbose": ("boolean", "Verbose"),
            },
        )
        self.assertNotIn("x-mcp-header", self.properties["note"])

    def test_annotations_satisfy_the_spec_constraints(self) -> None:
        names = [schema["x-mcp-header"] for schema in self.properties.values() if "x-mcp-header" in schema]
        token_chars = set("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
        for name in names:
            self.assertTrue(name and set(name) <= token_chars)
        self.assertEqual(len({name.lower() for name in names}), len(names))
        for schema in self.properties.values():
            if "x-mcp-header" in schema:
                self.assertIn(schema["type"], {"string", "integer", "boolean"})

    def test_tool_keeps_the_playmcp_metadata(self) -> None:
        self.assertIn("Unit Expert", self.tool["description"])
        self.assertIs(self.tool["annotations"]["readOnlyHint"], True)

    def test_header_param_properties_only_follows_properties(self) -> None:
        schema = {
            "properties": {
                "a": {"type": "string", "x-mcp-header": "A"},
                "nested": {"type": "object", "properties": {"b": {"type": "integer", "x-mcp-header": "B"}}},
                "list": {"type": "array", "items": {"type": "string", "x-mcp-header": "C"}},
            }
        }
        self.assertEqual(header_param_properties(schema), [(("a",), "A"), (("nested", "b"), "B")])


class HeaderValueDecodingTest(unittest.TestCase):
    def test_spec_examples(self) -> None:
        self.assertEqual(decode_mcp_header_value("us-west1"), "us-west1")
        self.assertEqual(decode_mcp_header_value("=?base64?SGVsbG8sIOS4lueVjA==?="), "Hello, 世界")
        self.assertEqual(decode_mcp_header_value("=?base64?IHBhZGRlZCA=?="), " padded ")
        self.assertEqual(decode_mcp_header_value("=?base64?bGluZTEKbGluZTI=?="), "line1\nline2")
        self.assertEqual(
            decode_mcp_header_value("=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?="), "=?base64?literal?="
        )

    def test_invalid_values(self) -> None:
        for value in ("café", "bell\x07", "=?base64?%%%?=", "=?base64?/w==?="):
            with self.subTest(value=value):
                self.assertIsNone(decode_mcp_header_value(value))

    def test_horizontal_tab_is_a_valid_field_character(self) -> None:
        # Spec "Value Encoding": visible ASCII, space and horizontal tab.
        self.assertEqual(decode_mcp_header_value("a\tb"), "a\tb")
        for value in ("a\x00b", "a\rb", "a\nb", "a\x7fb", "a\x1fb"):
            with self.subTest(value=value):
                self.assertIsNone(decode_mcp_header_value(value))

    def test_base64_is_as_strict_as_the_sdk(self) -> None:
        # mcp 2.3.0 decode_header_value: b64decode(validate=True) + canonical form.
        self.assertEqual(decode_mcp_header_value("=?base64?aGk=?="), "hi")
        self.assertEqual(decode_mcp_header_value("=?base64??="), "")
        for label, value in (
            ("no padding", "=?base64?aGk?="),
            ("url-safe alphabet", "=?base64?Pz8_?="),
            ("url-safe alphabet 2", "=?base64?Pj4-?="),
            ("non-zero trailing bits", "=?base64?aGl=?="),
            ("embedded whitespace", "=?base64?aG k=?="),
            ("excess padding", "=?base64?aGk==?="),
        ):
            with self.subTest(case=label):
                self.assertIsNone(decode_mcp_header_value(value))
        # The standard-alphabet spellings of the same bytes are fine.
        self.assertEqual(decode_mcp_header_value("=?base64?Pz8/?="), "???")
        self.assertEqual(decode_mcp_header_value("=?base64?Pj4+?="), ">>>")


class ParamHeaderValidationTest(unittest.TestCase):
    def assert_ok(self, result: tuple, expected_arguments: dict) -> None:
        status, _, payload = result
        self.assertEqual(status, 200, payload)
        assert payload is not None
        self.assertIs(payload["result"]["isError"], False)
        self.assertEqual(json.loads(payload["result"]["content"][0]["text"]), expected_arguments)

    def assert_mismatch(self, result: tuple) -> str:
        status, _, payload = result
        self.assertEqual(status, 400, payload)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_HEADER_MISMATCH)
        return payload["error"]["message"]

    def test_all_types_match(self) -> None:
        arguments = {"region": "us-west1", "greeting": "hello", "count": 42, "verbose": True, "note": "n"}
        self.assert_ok(
            call(
                arguments,
                {
                    "Mcp-Param-Region": "us-west1",
                    "Mcp-Param-Greeting": "hello",
                    "Mcp-Param-Count": "42",
                    "Mcp-Param-Verbose": "true",
                },
            ),
            arguments,
        )

    def test_header_names_are_case_insensitive(self) -> None:
        self.assert_ok(call({"region": "us-west1"}, {"mcp-param-region": "us-west1"}), {"region": "us-west1"})

    def test_non_ascii_value_via_base64_sentinel(self) -> None:
        arguments = {"greeting": "Hello, 世界"}
        self.assert_ok(call(arguments, {"Mcp-Param-Greeting": "=?base64?SGVsbG8sIOS4lueVjA==?="}), arguments)
        self.assert_ok(call({"greeting": "안녕"}, {"Mcp-Param-Greeting": sentinel("안녕")}), {"greeting": "안녕"})

    def test_ascii_value_may_also_arrive_encoded(self) -> None:
        self.assert_ok(call({"region": " padded "}, {"Mcp-Param-Region": sentinel(" padded ")}), {"region": " padded "})

    def test_value_that_looks_like_a_sentinel_must_be_encoded(self) -> None:
        literal = "=?base64?literal?="
        self.assert_ok(call({"region": literal}, {"Mcp-Param-Region": sentinel(literal)}), {"region": literal})
        # Sent raw, the server decodes it as a sentinel and the comparison fails.
        self.assert_mismatch(call({"region": literal}, {"Mcp-Param-Region": literal}))

    def test_missing_header_for_a_present_value_is_rejected(self) -> None:
        message = self.assert_mismatch(call({"region": "us-west1"}))
        self.assertIn("Mcp-Param-Region", message)

    def test_each_missing_header_is_reported(self) -> None:
        full = {
            "Mcp-Param-Region": "r",
            "Mcp-Param-Greeting": "g",
            "Mcp-Param-Count": "1",
            "Mcp-Param-Verbose": "false",
        }
        arguments = {"region": "r", "greeting": "g", "count": 1, "verbose": False}
        for dropped in full:
            with self.subTest(dropped=dropped):
                headers = {name: value for name, value in full.items() if name != dropped}
                self.assertIn(dropped, self.assert_mismatch(call(arguments, headers)))

    def test_null_or_absent_value_expects_no_header(self) -> None:
        self.assert_ok(call({}), {})
        self.assert_ok(call({"region": None, "count": None, "note": "x"}), {"region": None, "count": None, "note": "x"})

    def test_header_without_a_body_value_is_a_mismatch(self) -> None:
        self.assert_mismatch(call({}, {"Mcp-Param-Region": "us-west1"}))
        self.assert_mismatch(call({"region": None}, {"Mcp-Param-Region": "us-west1"}))

    def test_header_without_a_body_value_can_be_tolerated(self) -> None:
        config = {"modern": {"rejectUnexpectedParamHeader": False}}
        self.assert_ok(call({}, {"Mcp-Param-Region": "us-west1"}, config), {})
        self.assert_ok(
            call({"region": None}, {"Mcp-Param-Region": "us-west1"}, config), {"region": None}
        )
        # The other rules are not relaxed: invalid characters, a mismatch and a
        # missing header are still -32020.
        self.assert_mismatch(call({}, {"Mcp-Param-Region": "caf\u00e9"}, config))
        self.assert_mismatch(
            call({"region": "us-west1"}, {"Mcp-Param-Region": "us-east1"}, config)
        )
        self.assert_mismatch(call({"region": "us-west1"}, None, config))

    def test_string_mismatch(self) -> None:
        self.assert_mismatch(call({"region": "us-west1"}, {"Mcp-Param-Region": "us-east1"}))
        self.assert_mismatch(call({"region": "us-west1"}, {"Mcp-Param-Region": "US-WEST1"}))
        self.assert_mismatch(call({"greeting": "안녕"}, {"Mcp-Param-Greeting": sentinel("안녕!")}))

    def test_integer_is_compared_numerically(self) -> None:
        # Same gate as mcp 2.3.0 (_CANONICAL_DECIMAL): -?digits(.digits)?
        for header in ("42", "42.0", "42.000", "042", sentinel("42")):
            with self.subTest(header=header):
                self.assert_ok(call({"count": 42}, {"Mcp-Param-Count": header}), {"count": 42})
        self.assert_ok(call({"count": -7}, {"Mcp-Param-Count": "-7"}), {"count": -7})
        self.assert_ok(call({"count": 0}, {"Mcp-Param-Count": "-0"}), {"count": 0})
        for header in (
            "43", "42.5", "forty-two", "", "0x2A", "4_2", "NaN",
            "+42", "4.2e1", "42e0", "42.", ".42", "--42", sentinel(" 42"), sentinel("42\n"),
        ):
            with self.subTest(header=header):
                self.assert_mismatch(call({"count": 42}, {"Mcp-Param-Count": header}))

    def test_integral_float_body_value_is_an_integer(self) -> None:
        for header in ("42", "42.0"):
            with self.subTest(header=header):
                self.assert_ok(call({"count": 42.0}, {"Mcp-Param-Count": header}), {"count": 42.0})

    def test_surrounding_whitespace_is_not_part_of_the_value(self) -> None:
        self.assert_ok(call({"count": 42}, {"Mcp-Param-Count": " 42\t"}), {"count": 42})
        self.assert_ok(call({"region": "us-west1"}, {"Mcp-Param-Region": "us-west1  "}), {"region": "us-west1"})
        self.assert_ok(
            call({"greeting": "안녕"}, {"Mcp-Param-Greeting": sentinel("안녕") + " "}), {"greeting": "안녕"}
        )
        # A body value with edge whitespace has to come base64-encoded.
        self.assert_mismatch(call({"region": " us-west1 "}, {"Mcp-Param-Region": " us-west1 "}))

    def test_sentinel_without_padding_is_rejected(self) -> None:
        self.assert_ok(call({"region": "hi"}, {"Mcp-Param-Region": "=?base64?aGk=?="}), {"region": "hi"})
        message = self.assert_mismatch(call({"region": "hi"}, {"Mcp-Param-Region": "=?base64?aGk?="}))
        self.assertIn("Invalid Mcp-Param-Region", message)
        # URL-safe alphabet: "???" is Pz8/ in standard base64, Pz8_ url-safe.
        self.assert_ok(call({"region": "???"}, {"Mcp-Param-Region": "=?base64?Pz8/?="}), {"region": "???"})
        self.assert_mismatch(call({"region": "???"}, {"Mcp-Param-Region": "=?base64?Pz8_?="}))

    def test_large_integer_keeps_full_precision(self) -> None:
        big = 2**53 - 1
        self.assert_ok(call({"count": big}, {"Mcp-Param-Count": str(big)}), {"count": big})
        self.assert_mismatch(call({"count": big}, {"Mcp-Param-Count": str(big - 1)}))

    def test_boolean_is_lowercase_true_false(self) -> None:
        self.assert_ok(call({"verbose": True}, {"Mcp-Param-Verbose": "true"}), {"verbose": True})
        self.assert_ok(call({"verbose": False}, {"Mcp-Param-Verbose": "false"}), {"verbose": False})
        for header in ("True", "1", "false", "yes"):
            with self.subTest(header=header):
                self.assert_mismatch(call({"verbose": True}, {"Mcp-Param-Verbose": header}))

    def test_invalid_characters_are_rejected(self) -> None:
        # A raw non-ASCII header value (not base64-encoded) is malformed.
        message = self.assert_mismatch(call({"greeting": "café"}, {"Mcp-Param-Greeting": "café"}))
        self.assertIn("Invalid Mcp-Param-Greeting", message)
        self.assert_mismatch(call({"greeting": "x"}, {"Mcp-Param-Greeting": "=?base64?***?="}))

    def test_unannotated_argument_needs_no_header(self) -> None:
        self.assert_ok(call({"note": "free text 메모"}), {"note": "free text 메모"})

    def test_unrelated_param_headers_are_ignored(self) -> None:
        self.assert_ok(call({"note": "x"}, {"Mcp-Param-Unknown": "whatever"}), {"note": "x"})

    def test_wrong_argument_type_is_a_tool_execution_error(self) -> None:
        status, _, payload = call({"count": "many"}, {"Mcp-Param-Count": "many"})
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIs(payload["result"]["isError"], True)

    def test_tools_without_annotations_ignore_param_headers(self) -> None:
        payload, headers = modern_call(
            "convert_length",
            {"value": 1, "from_unit": "m", "to_unit": "cm"},
            param_headers={"Mcp-Param-Region": "us-west1"},
        )
        status, _, body = handle_json_rpc(payload, headers=headers)
        self.assertEqual(status, 200)
        assert body is not None
        self.assertIs(body["result"]["isError"], False)

    def test_lenient_mode_does_not_know_the_tool(self) -> None:
        status, _, payload = call({"region": "us-west1"}, config={"strict2026": False})
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIs(payload["result"]["isError"], True)

    def test_legacy_path_does_not_know_the_tool(self) -> None:
        status, _, payload = handle_json_rpc(
            legacy_request("tools/call", name=HEADER_PARAMS_TOOL, arguments={"region": "us-west1"})
        )
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIs(payload["result"]["isError"], True)
        self.assertIn("Unknown tool", payload["result"]["content"][0]["text"])


class ParamHeaderHttpTest(HttpServerTestCase):
    def test_round_trip_over_http(self) -> None:
        arguments = {"region": "us-west1", "greeting": "Hello, 世界", "count": 3, "verbose": False}
        payload, headers = modern_call(
            HEADER_PARAMS_TOOL,
            arguments,
            param_headers={
                "Mcp-Param-Region": "us-west1",
                "Mcp-Param-Greeting": sentinel("Hello, 世界"),
                "Mcp-Param-Count": "3",
                "Mcp-Param-Verbose": "false",
            },
        )
        status, _, body, _ = self.post(payload, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body["result"]["content"][0]["text"]), arguments)

    def test_raw_utf8_header_bytes_are_rejected(self) -> None:
        payload, headers = modern_call(HEADER_PARAMS_TOOL, {"greeting": "안녕"})
        # http.client refuses non-latin-1 header text, so send the UTF-8 bytes as
        # the latin-1 string a server sees on the wire.
        headers["Mcp-Param-Greeting"] = "안녕".encode("utf-8").decode("latin-1")
        status, _, body, _ = self.post(payload, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], ERR_HEADER_MISMATCH)

    def test_missing_header_over_http(self) -> None:
        payload, headers = modern_call(HEADER_PARAMS_TOOL, {"count": 3})
        status, _, body, _ = self.post(payload, headers=headers)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], ERR_HEADER_MISMATCH)

    def test_tools_list_advertises_the_annotations(self) -> None:
        status, _, body, _ = self.post(modern_request("tools/list"), headers=modern_headers("tools/list"))
        self.assertEqual(status, 200)
        tool = next(tool for tool in body["result"]["tools"] if tool["name"] == HEADER_PARAMS_TOOL)
        self.assertEqual(tool["inputSchema"]["properties"]["region"]["x-mcp-header"], "Region")


if __name__ == "__main__":
    unittest.main()
