"""Shared builders and an HTTP harness for the option tests."""

from __future__ import annotations

import base64
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any

from unit_expert_mcp.server import (
    PROTOCOL_VERSION_2026,
    Handler,
    encode_config_token,
    set_active_scenario,
)

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"
BOTH_ACCEPT = "application/json, text/event-stream"

ERR_HEADER_MISMATCH = -32020
ERR_MISSING_CAPABILITY = -32021
ERR_UNSUPPORTED_VERSION = -32022
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

CONVERT_ARGS = {"value": 1, "from_unit": "m", "to_unit": "cm"}


def sentinel(text: str) -> str:
    """The =?base64?...?= header form of a value."""
    return "=?base64?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def modern_request(
    method: str,
    request_id: Any = 1,
    *,
    capabilities: dict | None = None,
    version: str = PROTOCOL_VERSION_2026,
    **params: Any,
) -> dict:
    meta = {META_VERSION: version, META_CAPS: capabilities if capabilities is not None else {}}
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {**params, "_meta": meta}}


def modern_headers(method: str, *, version: str = PROTOCOL_VERSION_2026, **extra: str) -> dict[str, str]:
    return {"MCP-Protocol-Version": version, "Mcp-Method": method, **extra}


def modern_call(
    name: str,
    arguments: dict | None = None,
    request_id: Any = 1,
    *,
    capabilities: dict | None = None,
    param_headers: dict[str, str] | None = None,
    **params: Any,
) -> tuple[dict, dict[str, str]]:
    """(payload, headers) for a conformant modern tools/call."""
    payload = modern_request(
        "tools/call",
        request_id,
        capabilities=capabilities,
        name=name,
        arguments=arguments if arguments is not None else {},
        **params,
    )
    headers = modern_headers("tools/call", **{"Mcp-Name": name})
    headers.update(param_headers or {})
    return payload, headers


def legacy_initialize(request_id: Any = 1, version: str = "2025-03-26") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    }


def legacy_request(method: str, request_id: Any = 2, **params: Any) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        payload["params"] = params
    return payload


def parse_sse(raw: bytes) -> list[dict]:
    """JSON payloads of the data: lines of an SSE body, in order."""
    messages = []
    for block in raw.decode("utf-8").split("\n\n"):
        data = [line[len("data:"):].strip() for line in block.splitlines() if line.startswith("data:")]
        if data:
            messages.append(json.loads("\n".join(data)))
    return messages


class HttpServerTestCase(unittest.TestCase):
    """Runs the real Handler on a loopback port."""

    server: ThreadingHTTPServer
    port: int

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

    def path(self, config: dict | None = None) -> str:
        token = encode_config_token(config) if config else ""
        return f"/mcp?cfg={token}" if token else "/mcp"

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 10,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            if method == "GET" and response.status == 200:
                raw = response.read1(64)  # the legacy GET stream stays open
            else:
                raw = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, raw
        finally:
            conn.close()

    def post(
        self,
        payload: Any,
        *,
        config: dict | None = None,
        headers: dict[str, str] | None = None,
        accept: str | None = BOTH_ACCEPT,
        timeout: float = 10,
    ) -> tuple[int, dict[str, str], Any, bytes]:
        """POST /mcp. Returns (status, headers, parsed JSON or None, raw body)."""
        request_headers = {"Content-Type": "application/json"}
        if accept is not None:
            request_headers["Accept"] = accept
        request_headers.update(headers or {})
        status, response_headers, raw = self.request(
            "POST",
            self.path(config),
            headers=request_headers,
            body=json.dumps(payload).encode("utf-8"),
            timeout=timeout,
        )
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return status, response_headers, parsed, raw

    def modern_post(self, method: str, *, config: dict | None = None, **kwargs: Any):
        return self.post(
            modern_request(method), config=config, headers=modern_headers(method), **kwargs
        )
