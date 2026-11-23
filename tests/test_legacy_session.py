"""Legacy session options (``session.validate``, ``session.issueId``,
``expireSessions``) and the initialize protocol-version choices."""

from __future__ import annotations

import json
import unittest

from mcp_test_support import HttpServerTestCase, legacy_initialize, legacy_request, modern_headers, modern_request
from unit_expert_mcp.server import (
    PROTOCOL_VERSION_CHOICES,
    SESSIONS,
    expire_sessions,
    handle_json_rpc,
    inspect_snapshot,
    normalize_config,
)

VALIDATE = {"session": {"validate": True}}


def session_error(message: str) -> dict:
    return {"jsonrpc": "2.0", "id": "server-error", "error": {"code": -32600, "message": message}}


# Bodies of the official Python SDK 1.27.2 (scripts/sdk1_legacy_server.py with
# MCP_JSON=1, observed 2026-11-23), byte for byte.
MISSING_SESSION = session_error("Bad Request: Missing session ID")
UNKNOWN_SESSION = session_error("Session not found")
TERMINATED_SESSION = session_error("Not Found: Session has been terminated")
MISSING_SESSION_RAW = (
    b'{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,'
    b'"message":"Bad Request: Missing session ID"}}'
)
UNKNOWN_SESSION_RAW = (
    b'{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Session not found"}}'
)
TERMINATED_SESSION_RAW = (
    b'{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,'
    b'"message":"Not Found: Session has been terminated"}}'
)


class SessionDispatchTest(unittest.TestCase):
    def initialize(self, config: dict | None = None) -> str:
        status, headers, _ = handle_json_rpc(legacy_initialize(), config=config)
        self.assertEqual(status, 200)
        return headers["Mcp-Session-Id"]

    def test_defaults_keep_the_old_behaviour(self) -> None:
        config = normalize_config({})
        self.assertEqual(config["session"], {"validate": False, "issueId": True})
        # No session header, unknown session header: both still served.
        for headers in (None, {"Mcp-Session-Id": "never-issued"}):
            status, _, payload = handle_json_rpc(legacy_request("tools/list"), headers=headers)
            self.assertEqual(status, 200)
            assert payload is not None
            self.assertEqual(len(payload["result"]["tools"]), 6)

    def test_validate_accepts_an_issued_session(self) -> None:
        session_id = self.initialize(VALIDATE)
        status, _, payload = handle_json_rpc(
            legacy_request("tools/list"), config=VALIDATE, headers={"mcp-session-id": session_id}
        )
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIn("tools", payload["result"])

    def test_validate_rejects_a_missing_session_header_with_400(self) -> None:
        for payload in (
            legacy_request("tools/list"),
            legacy_request("tools/call", name="convert_length"),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ):
            with self.subTest(method=payload["method"]):
                status, _, body = handle_json_rpc(payload, config=VALIDATE)
                self.assertEqual(status, 400)
                self.assertEqual(body, MISSING_SESSION)
                # An empty header value is a missing session id too.
                status, _, body = handle_json_rpc(
                    payload, config=VALIDATE, headers={"Mcp-Session-Id": ""}
                )
                self.assertEqual((status, body), (400, MISSING_SESSION))

    def test_validate_rejects_an_unknown_session_with_404(self) -> None:
        status, _, body = handle_json_rpc(
            legacy_request("tools/list"), config=VALIDATE, headers={"Mcp-Session-Id": "never-issued"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(body, UNKNOWN_SESSION)

    def test_initialize_never_needs_a_session(self) -> None:
        self.assertTrue(self.initialize(VALIDATE))

    def test_expire_sessions_turns_every_issued_session_into_404(self) -> None:
        first = self.initialize(VALIDATE)
        second = self.initialize(VALIDATE)
        self.assertGreaterEqual(expire_sessions(), 2)
        self.assertEqual(expire_sessions(), 0)
        for session_id in (first, second):
            status, _, body = handle_json_rpc(
                legacy_request("tools/list"), config=VALIDATE, headers={"Mcp-Session-Id": session_id}
            )
            self.assertEqual((status, body), (404, UNKNOWN_SESSION))
        # Reconnecting hands out a session that works again.
        fresh = self.initialize(VALIDATE)
        status, _, _ = handle_json_rpc(
            legacy_request("tools/list"), config=VALIDATE, headers={"Mcp-Session-Id": fresh}
        )
        self.assertEqual(status, 200)

    def test_issue_id_off_returns_no_session_header(self) -> None:
        before = len(SESSIONS)
        status, headers, payload = handle_json_rpc(legacy_initialize(), config={"session": {"issueId": False}})
        self.assertEqual(status, 200)
        self.assertNotIn("Mcp-Session-Id", headers)
        self.assertEqual(len(SESSIONS), before)
        assert payload is not None
        self.assertEqual(payload["result"]["protocolVersion"], "2025-03-26")

    def test_issue_id_off_disables_validation(self) -> None:
        config = {"session": {"issueId": False, "validate": True}}
        status, _, payload = handle_json_rpc(legacy_request("tools/list"), config=config)
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIn("tools", payload["result"])

    def test_config_validation(self) -> None:
        for bad in ({"session": {"validate": "yes"}}, {"session": {"issueId": 1}}, {"session": True}):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    normalize_config(bad)


class InitializeVersionTest(unittest.TestCase):
    def test_choices_include_versions_a_gateway_must_refuse(self) -> None:
        versions = [version for version, _ in PROTOCOL_VERSION_CHOICES]
        for version in ("2024-11-05", "2026-07-28", "2024-03-26"):
            self.assertIn(version, versions)

    def test_initialize_answers_with_the_configured_version(self) -> None:
        for version, _ in PROTOCOL_VERSION_CHOICES:
            with self.subTest(version=version):
                status, headers, payload = handle_json_rpc(
                    legacy_initialize(), config={"initialize": {"protocolVersion": version}}
                )
                self.assertEqual(status, 200)
                self.assertIn("Mcp-Session-Id", headers)
                assert payload is not None
                self.assertEqual(payload["result"]["protocolVersion"], version)
                self.assertNotIn("resultType", payload["result"])

    def test_header_scenario_selects_a_version(self) -> None:
        for version in ("2026-07-28", "2024-11-05", "2025-11-25"):
            with self.subTest(version=version):
                _, _, payload = handle_json_rpc(legacy_initialize(), scenario=f"initialize-version-{version}")
                assert payload is not None
                self.assertEqual(payload["result"]["protocolVersion"], version)

    def test_default_never_negotiates_2026(self) -> None:
        _, _, payload = handle_json_rpc(legacy_initialize(version="2026-07-28"))
        assert payload is not None
        self.assertEqual(payload["result"]["protocolVersion"], "2025-03-26")


class SessionHttpTest(HttpServerTestCase):
    def initialize(self, config: dict | None) -> str:
        status, headers, _, _ = self.post(legacy_initialize(), config=config)
        self.assertEqual(status, 200)
        return headers["mcp-session-id"]

    def list_tools(self, config: dict | None, session_id: str | None):
        headers = {"Mcp-Session-Id": session_id} if session_id else {}
        return self.post(legacy_request("tools/list"), config=config, headers=headers)

    def test_session_lifecycle_with_delete(self) -> None:
        session_id = self.initialize(VALIDATE)
        self.assertEqual(self.list_tools(VALIDATE, session_id)[0], 200)

        status, _, raw = self.request("DELETE", self.path(VALIDATE), headers={"Mcp-Session-Id": session_id})
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"")

        # Every later request with the ended id: 404 "terminated", not "not found".
        status, headers, _, raw = self.list_tools(VALIDATE, session_id)
        self.assertEqual((status, raw), (404, TERMINATED_SESSION_RAW))
        self.assertEqual(headers["content-type"], "application/json")
        status, _, _, raw = self.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            config=VALIDATE,
            headers={"Mcp-Session-Id": session_id},
        )
        self.assertEqual((status, raw), (404, TERMINATED_SESSION_RAW))
        status, _, raw = self.request("DELETE", self.path(VALIDATE), headers={"Mcp-Session-Id": session_id})
        self.assertEqual((status, raw), (404, TERMINATED_SESSION_RAW))
        status, _, raw = self.request(
            "GET",
            self.path(VALIDATE),
            headers={"Accept": "text/event-stream", "Mcp-Session-Id": session_id},
        )
        self.assertEqual((status, raw), (404, TERMINATED_SESSION_RAW))

    def test_expire_sessions_forgets_terminated_sessions(self) -> None:
        session_id = self.initialize(VALIDATE)
        self.request("DELETE", self.path(VALIDATE), headers={"Mcp-Session-Id": session_id})
        expire_sessions()
        status, _, _, raw = self.list_tools(VALIDATE, session_id)
        self.assertEqual((status, raw), (404, UNKNOWN_SESSION_RAW))

    def test_delete_without_a_session_header_is_400(self) -> None:
        status, headers, raw = self.request("DELETE", self.path(VALIDATE))
        self.assertEqual((status, raw), (400, MISSING_SESSION_RAW))
        self.assertEqual(headers["content-type"], "application/json")

    def test_delete_of_an_unknown_session_is_404(self) -> None:
        status, _, raw = self.request(
            "DELETE", self.path(VALIDATE), headers={"Mcp-Session-Id": "never-issued"}
        )
        self.assertEqual((status, raw), (404, UNKNOWN_SESSION_RAW))
        self.assertEqual(inspect_snapshot()[0]["responseBody"], UNKNOWN_SESSION)

    def test_delete_without_validation_stays_405(self) -> None:
        session_id = self.initialize(None)
        status, _, _ = self.request("DELETE", "/mcp", headers={"Mcp-Session-Id": session_id})
        self.assertEqual(status, 405)
        # The session survives.
        self.assertEqual(self.list_tools(VALIDATE, session_id)[0], 200)

    def test_missing_and_unknown_sessions_on_the_wire(self) -> None:
        status, headers, _, raw = self.list_tools(VALIDATE, None)
        self.assertEqual((status, raw), (400, MISSING_SESSION_RAW))
        self.assertEqual(headers["content-type"], "application/json")
        status, headers, _, raw = self.list_tools(VALIDATE, "never-issued")
        self.assertEqual((status, raw), (404, UNKNOWN_SESSION_RAW))
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(inspect_snapshot()[0]["injected"], ["session.validate"])

    def test_get_stream_checks_the_session(self) -> None:
        session_id = self.initialize(VALIDATE)
        base = {"Accept": "text/event-stream"}
        status, _, _ = self.request("GET", self.path(VALIDATE), headers={**base, "Mcp-Session-Id": session_id})
        self.assertEqual(status, 200)
        status, headers, raw = self.request(
            "GET", self.path(VALIDATE), headers={**base, "Mcp-Session-Id": "never-issued"}
        )
        self.assertEqual((status, raw), (404, UNKNOWN_SESSION_RAW))
        self.assertEqual(headers["content-type"], "application/json")
        status, _, raw = self.request("GET", self.path(VALIDATE), headers=base)
        self.assertEqual((status, raw), (400, MISSING_SESSION_RAW))
        # Without validation any id opens the stream, as before.
        status, _, _ = self.request("GET", "/mcp", headers={**base, "Mcp-Session-Id": "never-issued"})
        self.assertEqual(status, 200)

    def test_get_without_validation_keeps_the_plain_text_400(self) -> None:
        status, headers, raw = self.request("GET", "/mcp", headers={"Accept": "text/event-stream"})
        self.assertEqual((status, raw), (400, b"Session ID required in mcp-session-id header"))
        self.assertTrue(headers["content-type"].startswith("text/plain"))

    def test_expire_sessions_action(self) -> None:
        session_id = self.initialize(VALIDATE)
        status, _, raw = self.request(
            "POST",
            "/scenario",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"action": "expireSessions"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertIs(body["ok"], True)
        self.assertEqual(body["action"], "expireSessions")
        self.assertGreaterEqual(body["expired"], 1)

        status, _, _, raw = self.list_tools(VALIDATE, session_id)
        self.assertEqual((status, raw), (404, UNKNOWN_SESSION_RAW))
        fresh = self.initialize(VALIDATE)
        self.assertNotEqual(fresh, session_id)
        self.assertEqual(self.list_tools(VALIDATE, fresh)[0], 200)

    def test_expire_sessions_does_not_change_the_active_config(self) -> None:
        self.request(
            "POST",
            "/scenario",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"config": VALIDATE}).encode("utf-8"),
        )
        self.request(
            "POST",
            "/scenario",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"action": "expireSessions"}).encode("utf-8"),
        )
        # The active config (validate on) is still in force.
        status, _, payload, _ = self.list_tools(None, "never-issued")
        self.assertEqual((status, payload), (404, UNKNOWN_SESSION))

    def test_unknown_action_is_rejected(self) -> None:
        status, _, raw = self.request(
            "POST",
            "/scenario",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"action": "selfDestruct"}).encode("utf-8"),
        )
        self.assertEqual(status, 400)
        self.assertIs(json.loads(raw)["ok"], False)

    def test_issue_id_off_on_the_wire(self) -> None:
        status, headers, payload, _ = self.post(legacy_initialize(), config={"session": {"issueId": False}})
        self.assertEqual(status, 200)
        self.assertNotIn("mcp-session-id", headers)
        self.assertIn("result", payload)

    def test_modern_requests_ignore_session_validation(self) -> None:
        status, headers, payload, _ = self.post(
            modern_request("tools/list"), config=VALIDATE, headers=modern_headers("tools/list")
        )
        self.assertEqual(status, 200)
        self.assertNotIn("mcp-session-id", headers)
        self.assertEqual(payload["result"]["resultType"], "complete")


if __name__ == "__main__":
    unittest.main()
