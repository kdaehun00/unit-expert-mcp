"""End-to-end tests for the mock OAuth Authorization Server + Resource Server.

These exercise the HTTP Handler (routes, PKCE, JWT, 401 challenge) against a real
server bound to an ephemeral port, since the auth gate lives in the HTTP layer
rather than in handle_json_rpc.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from unit_expert_mcp.server import Handler, default_config, encode_config_token

MCP_ACCEPT = "application/json, text/event-stream"


def pkce_pair() -> tuple[str, str]:
    verifier = "unit-expert-test-verifier-0123456789abcdef"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    return verifier, challenge


def cfg_for_auth(mode: str) -> str:
    config = default_config()
    config["auth"]["mode"] = mode
    return encode_config_token(config)


class OAuthServerTest(unittest.TestCase):
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

    # -- low-level request helper -------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        conn.close()
        return response.status, response_headers, raw

    def mcp_call(self, cfg: str, payload: dict, token: str | None = None):
        headers = {"Content-Type": "application/json", "Accept": MCP_ACCEPT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.request(
            "POST", f"/mcp?cfg={cfg}", headers=headers, body=json.dumps(payload).encode()
        )

    def obtain_token(self) -> str:
        """Run the full authorization-code + PKCE flow and return an access token."""
        redirect_uri = "http://localhost/callback"
        verifier, challenge = pkce_pair()

        status, _, raw = self.request(
            "POST",
            "/register",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"redirect_uris": [redirect_uri]}).encode(),
        )
        self.assertEqual(status, 201)
        client_id = json.loads(raw)["client_id"]

        form = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": "xyz",
                "scope": "mcp",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "action": "approve",
                "consent": "1",
            }
        ).encode()
        status, headers, _ = self.request(
            "POST",
            "/authorize",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form,
        )
        self.assertEqual(status, 302)
        location = headers["location"]
        query = parse_qs(urlparse(location).query)
        self.assertEqual(query["state"], ["xyz"])
        code = query["code"][0]

        token_form = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            }
        ).encode()
        status, _, raw = self.request(
            "POST",
            "/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=token_form,
        )
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(body["token_type"], "Bearer")
        return body["access_token"]

    # -- discovery -----------------------------------------------------------
    def test_protected_resource_metadata(self) -> None:
        status, _, raw = self.request("GET", "/.well-known/oauth-protected-resource")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertTrue(body["resource"].endswith("/mcp"))
        self.assertEqual(len(body["authorization_servers"]), 1)

    def test_authorization_server_metadata(self) -> None:
        status, _, raw = self.request("GET", "/.well-known/oauth-authorization-server")
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertTrue(body["authorization_endpoint"].endswith("/authorize"))
        self.assertTrue(body["token_endpoint"].endswith("/token"))
        self.assertIn("S256", body["code_challenge_methods_supported"])

    # -- off mode ------------------------------------------------------------
    def test_off_mode_allows_anonymous_tool_call(self) -> None:
        status, _, raw = self.mcp_call(
            cfg_for_auth("off"),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "convert_length", "arguments": {"value": 1, "from_unit": "m", "to_unit": "cm"}},
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["result"]["isError"])

    # -- oauth mode ----------------------------------------------------------
    def test_oauth_mode_blocks_without_token(self) -> None:
        status, headers, _ = self.mcp_call(
            cfg_for_auth("oauth"),
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        self.assertEqual(status, 401)
        self.assertIn("resource_metadata", headers.get("www-authenticate", ""))

    def test_oauth_mode_allows_with_token(self) -> None:
        token = self.obtain_token()
        status, _, raw = self.mcp_call(
            cfg_for_auth("oauth"),
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)["result"]["tools"]), 6)

    def test_invalid_token_is_rejected(self) -> None:
        status, _, _ = self.mcp_call(
            cfg_for_auth("oauth"),
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            token="not-a-real-jwt",
        )
        self.assertEqual(status, 401)

    # -- mixed mode ----------------------------------------------------------
    def test_mixed_mode_lists_tools_anonymously(self) -> None:
        status, _, raw = self.mcp_call(
            cfg_for_auth("mixed-oauth"),
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)["result"]["tools"]), 6)

    def test_mixed_mode_public_tool_without_token(self) -> None:
        status, _, raw = self.mcp_call(
            cfg_for_auth("mixed-oauth"),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_supported_units", "arguments": {}},
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["result"]["isError"])

    def test_mixed_mode_protected_tool_requires_token(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "convert_length", "arguments": {"value": 1, "from_unit": "m", "to_unit": "cm"}},
        }
        status, headers, _ = self.mcp_call(cfg_for_auth("mixed-oauth"), payload)
        self.assertEqual(status, 401)
        self.assertIn("resource_metadata", headers.get("www-authenticate", ""))

        token = self.obtain_token()
        status, _, raw = self.mcp_call(cfg_for_auth("mixed-oauth"), payload, token=token)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["result"]["isError"])

    # -- token endpoint edge cases ------------------------------------------
    def test_token_rejects_pkce_mismatch(self) -> None:
        redirect_uri = "http://localhost/callback"
        _, challenge = pkce_pair()
        form = urlencode(
            {
                "response_type": "code",
                "client_id": "mcp-client-x",
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "action": "approve",
                "consent": "1",
            }
        ).encode()
        _, headers, _ = self.request(
            "POST",
            "/authorize",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form,
        )
        code = parse_qs(urlparse(headers["location"]).query)["code"][0]
        token_form = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": "mcp-client-x",
                "code_verifier": "wrong-verifier",
            }
        ).encode()
        status, _, raw = self.request(
            "POST",
            "/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=token_form,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"], "invalid_grant")

    def test_authorize_without_consent_denies(self) -> None:
        form = urlencode(
            {
                "response_type": "code",
                "client_id": "mcp-client-x",
                "redirect_uri": "http://localhost/callback",
                "state": "s1",
                "action": "approve",
            }
        ).encode()
        status, headers, _ = self.request(
            "POST",
            "/authorize",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form,
        )
        self.assertEqual(status, 302)
        query = parse_qs(urlparse(headers["location"]).query)
        self.assertEqual(query["error"], ["access_denied"])


if __name__ == "__main__":
    unittest.main()
