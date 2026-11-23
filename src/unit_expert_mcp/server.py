"""Minimal Streamable HTTP MCP server for PlayMCP."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import socket
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

SERVER_NAME = "unitExpert"
SERVER_VERSION = "1.0.0"
SERVICE_NAME = "Unit Expert(단위전문가)"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-03-26", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")

# 2026-07-28 stateless protocol constants.
PROTOCOL_VERSION_2026 = "2026-07-28"
# Versions reachable over the legacy (initialize handshake + session id) transport.
# The legacy path must only ever negotiate one of these — see handle_json_rpc.
LEGACY_PROTOCOL_VERSIONS = tuple(
    version for version in SUPPORTED_PROTOCOL_VERSIONS if version != PROTOCOL_VERSION_2026
)
LATEST_LEGACY_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[-1]
# Versions the modern (stateless) path implements. Spec: a request for any other
# version gets 400 + -32022 with data.supported listing exactly these.
MODERN_PROTOCOL_VERSIONS = (PROTOCOL_VERSION_2026,)
# Per-request _meta envelope keys (spec: basic/index#meta).
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"
SUBSCRIPTIONS_LISTEN_METHOD = "subscriptions/listen"
SUBSCRIPTIONS_ACKNOWLEDGED_METHOD = "notifications/subscriptions/acknowledged"
# MCP-defined JSON-RPC error codes introduced in 2026-07-28.
ERR_HEADER_MISMATCH = -32020
ERR_MISSING_CAPABILITY = -32021
ERR_UNSUPPORTED_VERSION = -32022
ERR_INVALID_PARAMS = -32602
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_REQUEST = -32600
ERR_INTERNAL = -32603
# Methods the modern path implements. Anything else (including methods removed
# in 2026-07-28 such as initialize and ping) is 404 + -32601.
MODERN_SUPPORTED_METHODS = (
    "server/discover",
    "tools/list",
    "tools/call",
    "subscriptions/listen",
)
# Base64 sentinel for Mcp-Name / Mcp-Param-* header values (case-sensitive).
BASE64_SENTINEL_PREFIX = "=?base64?"
BASE64_SENTINEL_SUFFIX = "?="
MCP_PARAM_HEADER_PREFIX = "mcp-param-"
# Schema keyword that mirrors a tool argument into an Mcp-Param-{name} header.
X_MCP_HEADER = "x-mcp-header"
# Test-only tools that exist on the modern path only (see modern_test_tools).
HEADER_PARAMS_TOOL = "echo_header_params"
MRTR_TOOL = "confirm_action"
MRTR_INPUT_KEY = "confirm"
MRTR_STATE_TTL_SECONDS = 600
# method -> params key mirrored into the Mcp-Name header.
NAME_BEARING_METHODS = {
    "tools/call": "name",
    "resources/read": "uri",
    "prompts/get": "name",
}
PROTOCOL_VERSION_CHOICES = (
    ("2024-03-26", "2024-03-26 (스펙에 아예 없는 버전)"),
    ("2024-11-05", "2024-11-05 (최소 지원 미만)"),
    ("2025-03-26", "2025-03-26 (최소 지원)"),
    ("2025-06-18", "2025-06-18 (지원)"),
    ("2025-11-25", "2025-11-25 (최대 지원)"),
    ("2026-07-28", "2026-07-28 (2026 방식의 버전 - initialize 응답으로는 틀린 값)"),
)
TEST_SCENARIO_HEADER = "X-MCP-Test-Scenario"
DEFAULT_DELAY_SECONDS = 5.0
PUBLIC_MCP_URL = "https://unit-expert-mcp.onrender.com/mcp"
CONFIG_QUERY_PARAM = "cfg"

SESSIONS: set[str] = set()
# Session ids ended with DELETE /mcp (session.validate). The official Python SDK
# 1.27.2 answers them differently from ids it never issued.
TERMINATED_SESSIONS: set[str] = set()
SCENARIO_LOCK = Lock()
ACTIVE_SCENARIO = "ok"
ACTIVE_CONFIG: dict[str, Any] = {}

# In-memory ring buffer of recent /mcp exchanges, surfaced by the /inspect page
# so the 2026 wire (headers + _meta envelope + response) is visible live.
INSPECT_LOCK = Lock()
INSPECT_LOG: deque[dict[str, Any]] = deque(maxlen=200)
INSPECT_SEQ = 0
# Request header naming who sent the request (for example the /scenario page).
# Read for the inspection log only; it never changes an MCP response.
INSPECT_SOURCE_HEADER = "X-Unit-Expert-Source"
# Only these request headers are interesting for MCP wire inspection.
INSPECT_HEADER_PREFIXES = ("mcp-", "x-mcp-")
INSPECT_HEADER_NAMES = ("accept", "content-type", "authorization")


def record_exchange(
    method_http: str,
    path: str,
    request_headers: Any,
    request_body: Any,
    status: int,
    response_body: Any,
    *,
    raw_response: bool = False,
    response_headers: dict[str, str] | None = None,
    server_era: str | None = None,
    injected: list[str] | None = None,
    query: str = "",
    config_source: str | None = None,
    started: float | None = None,
) -> dict[str, Any]:
    """Append one /mcp exchange to the inspection ring buffer and return the entry.

    ``injected`` names the config options that shaped this response (for example
    ``toolCall.mode=sse``), so /inspect shows why a response looks the way it does.
    ``query`` is the raw query string of the request URL, ``config_source`` where
    the applied config came from ("url" | "header" | "global") and ``started``
    the ``time.monotonic()`` value taken when the request arrived.
    """
    global INSPECT_SEQ
    if raw_response and isinstance(response_body, str):
        # Developer-authored bodies may be deliberately broken JSON; keep the
        # original text when it doesn't parse.
        try:
            response_body = json.loads(response_body)
        except json.JSONDecodeError:
            pass
    headers = {}
    for name, value in (request_headers.items() if request_headers else []):
        lowered = name.lower()
        if lowered in INSPECT_HEADER_NAMES or lowered.startswith(INSPECT_HEADER_PREFIXES):
            headers[name] = value
    jsonrpc_method = request_body.get("method") if isinstance(request_body, dict) else None
    era = request_era(request_body, dict(request_headers) if request_headers else None)
    source = request_headers.get(INSPECT_SOURCE_HEADER) if request_headers else None
    source = source.strip() if isinstance(source, str) else None
    now = datetime.now(timezone.utc)
    entry: dict[str, Any] = {}
    with INSPECT_LOCK:
        INSPECT_SEQ += 1
        entry.update(
            {
                "seq": INSPECT_SEQ,
                "time": now.strftime("%H:%M:%S"),
                "httpMethod": method_http,
                "path": path,
                "era": era,
                "method": jsonrpc_method,
                "requestHeaders": headers,
                "requestBody": request_body,
                "status": status,
                "responseBody": response_body,
                "rawResponse": raw_response,
                "responseHeaders": dict(response_headers or {}),
                "serverEra": server_era,
                "injected": list(injected or []),
                "timestamp": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "durationMs": elapsed_ms(started),
                "query": query,
                "cfg": parse_qs(query).get(CONFIG_QUERY_PARAM, [None])[0],
                "configSource": config_source,
                "source": source or None,
            }
        )
        INSPECT_LOG.appendleft(entry)
    return entry


def elapsed_ms(started: float | None) -> float | None:
    """Milliseconds since a ``time.monotonic()`` value, or None when unknown."""
    if started is None:
        return None
    return round((time.monotonic() - started) * 1000, 1)


def finish_exchanges(entries: list[dict[str, Any]], started: float | None) -> None:
    """Set durationMs of recorded entries once the response has been written."""
    if not entries or started is None:
        return
    duration = elapsed_ms(started)
    with INSPECT_LOCK:
        for entry in entries:
            entry["durationMs"] = duration


def inspect_snapshot() -> list[dict[str, Any]]:
    with INSPECT_LOCK:
        return list(INSPECT_LOG)


def inspect_query(raw_query: str) -> list[dict[str, Any]]:
    """Entries of the inspection log (newest first) filtered by /inspect/log's query.

    ``since=<seq>`` keeps entries with a larger seq, ``cfg=<token>`` those whose
    URL carried that token (an empty value keeps requests without a cfg token)
    and ``limit=<n>`` the newest n of what is left.
    """
    params = parse_qs(raw_query, keep_blank_values=True)

    def number(name: str) -> int | None:
        if name not in params:
            return None
        text = params[name][0].strip()
        if not text.isascii() or not text.isdigit():
            raise ValueError(f"{name} must be a non-negative integer")
        return int(text)

    since = number("since")
    limit = number("limit")
    entries = inspect_snapshot()
    if since is not None:
        entries = [entry for entry in entries if entry["seq"] > since]
    if CONFIG_QUERY_PARAM in params:
        token = params[CONFIG_QUERY_PARAM][0] or None
        entries = [entry for entry in entries if entry.get("cfg") == token]
    if limit is not None:
        entries = entries[:limit]
    return entries


def inspect_groups_requested(raw_query: str) -> bool:
    """``groups=1`` in /inspect/log's query asks for inspect_groups() as well."""
    return parse_qs(raw_query, keep_blank_values=True).get("groups", [""])[0] == "1"


def inspect_groups() -> list[dict[str, Any]]:
    """The whole inspection log summed up per MCP address, newest address first.

    One entry per cfg token (``cfg`` None: the address without a config):
    ``count`` requests, ``screen`` of them sent by a control page, and the seq
    and timestamp of the newest one. OPTIONS requests are not counted.
    """
    groups: dict[str | None, dict[str, Any]] = {}
    for entry in inspect_snapshot():
        if entry.get("httpMethod") == "OPTIONS":
            continue
        token = entry.get("cfg") or None
        group = groups.get(token)
        if group is None:
            group = groups[token] = {
                "cfg": token,
                "count": 0,
                "screen": 0,
                "lastSeq": entry["seq"],
                "lastTimestamp": entry.get("timestamp"),
            }
        group["count"] += 1
        if entry.get("source"):
            group["screen"] += 1
        if entry["seq"] > group["lastSeq"]:
            group["lastSeq"] = entry["seq"]
            group["lastTimestamp"] = entry.get("timestamp")
    return sorted(groups.values(), key=lambda group: group["lastSeq"], reverse=True)


# ---------------------------------------------------------------------------
# Mock OAuth 2.1 Authorization Server + Resource Server.
#
# This server issues its OWN tokens. The token represents OUR user (sub), minted
# for the OAuth client that registered with us (client_id) — this is the correct
# "the MCP owns its auth" shape, not "delegate to an external IdP as the AS".
# Everything is in-memory: auth codes live for seconds, access tokens are
# self-verifying HS256 JWTs so we never have to store them.
# ---------------------------------------------------------------------------
OAUTH_LOCK = Lock()
# client_id -> registration metadata (redirect_uris, etc.).
REGISTERED_CLIENTS: dict[str, dict[str, Any]] = {}
# authorization code -> {client_id, redirect_uri, code_challenge, method, sub, exp}.
AUTH_CODES: dict[str, dict[str, Any]] = {}

# HS256 signing secret. A fixed default keeps the mock reproducible across
# restarts; override with MCP_OAUTH_SECRET in real deployments.
JWT_SECRET = os.getenv("MCP_OAUTH_SECRET", "unit-expert-mock-oauth-secret")
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
AUTH_CODE_TTL_SECONDS = 300
# Payload claim separating the two token kinds (distinct from the JWT *header*'s
# "typ": "JWT"). Both are signed with the same secret, so without this claim a
# refresh token would pass as a 30-day all-access bearer token. Tokens minted
# before this claim existed carry none and are read as access tokens.
TOKEN_TYPE_CLAIM = "typ"
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"
# Fixed demo identity handed out by the one-click login. A real login layer
# (including "sign in with Kakao" as one option) would resolve a real user here.
DEMO_USER_SUB = "demo-user"
OAUTH_SCOPE = "mcp"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(f"{text}{padding}".encode("ascii"))


def issue_access_token(claims: dict[str, Any], ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS) -> str:
    """Mint a self-verifying HS256 JWT — no server-side storage required."""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {**claims, "iat": now, "exp": now + ttl_seconds}
    segments = [
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
    segments.append(_b64url_encode(signature))
    return ".".join(segments)


def verify_access_token(token: str | None) -> dict[str, Any] | None:
    """Return the JWT claims if signature + expiry are valid, else None."""
    if not token:
        return None
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
    except ValueError:
        return None
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    expected = hmac.new(JWT_SECRET.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        actual = _b64url_decode(signature_segment)
    except (binascii.Error, ValueError):
        return None
    if not hmac.compare_digest(actual, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_segment))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def issue_refresh_token(claims: dict[str, Any]) -> str:
    """Mint a long-lived refresh token — same self-verifying JWT, longer TTL.

    Stateless like the access token: nothing is stored, so these cannot be
    revoked or rotated. Adequate for a mock AS, not for a real one.
    """
    return issue_access_token(
        {**claims, TOKEN_TYPE_CLAIM: TOKEN_TYPE_REFRESH}, REFRESH_TOKEN_TTL_SECONDS
    )


def verify_bearer_access_token(token: str | None) -> dict[str, Any] | None:
    """Verify a Bearer token presented for resource access.

    Rejects refresh tokens: they are valid signatures with a far longer life, so
    letting one through here would hand out 30-day access to /mcp.
    """
    claims = verify_access_token(token)
    if claims is None or claims.get(TOKEN_TYPE_CLAIM) == TOKEN_TYPE_REFRESH:
        return None
    return claims


def bearer_token(headers: Any) -> str | None:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    raw = headers.get("Authorization") or headers.get("authorization") or ""
    parts = raw.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def pkce_matches(verifier: str, challenge: str, method: str) -> bool:
    """Validate a PKCE code_verifier against the stored challenge."""
    if not challenge:
        return True
    if method == "S256":
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return hmac.compare_digest(_b64url_encode(digest), challenge)
    # "plain" (or unspecified) — direct comparison.
    return hmac.compare_digest(verifier, challenge)


def register_client(metadata: dict[str, Any]) -> dict[str, Any]:
    """Dynamic Client Registration (RFC 7591). Public client, no secret."""
    client_id = f"mcp-client-{uuid.uuid4().hex}"
    redirect_uris = metadata.get("redirect_uris")
    if not isinstance(redirect_uris, list):
        redirect_uris = []
    record = {
        "client_id": client_id,
        "redirect_uris": [uri for uri in redirect_uris if isinstance(uri, str)],
        "client_name": metadata.get("client_name"),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    with OAUTH_LOCK:
        REGISTERED_CLIENTS[client_id] = record
    return record


def create_auth_code(entry: dict[str, Any]) -> str:
    code = uuid.uuid4().hex
    entry = {**entry, "exp": int(time.time()) + AUTH_CODE_TTL_SECONDS}
    with OAUTH_LOCK:
        AUTH_CODES[code] = entry
    return code


def consume_auth_code(code: str) -> dict[str, Any] | None:
    """Single-use: pop the code and return it only if unexpired."""
    with OAUTH_LOCK:
        entry = AUTH_CODES.pop(code, None)
    if entry is None:
        return None
    if int(entry.get("exp", 0)) < int(time.time()):
        return None
    return entry


def _append_query(url: str, params: dict[str, str | None]) -> str:
    """Append query parameters to a redirect URL, dropping None values."""
    pairs = [(key, value) for key, value in params.items() if value is not None]
    if not pairs:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(pairs)}"


def protected_resource_metadata(base_url: str) -> dict[str, Any]:
    """RFC 9728 — advertises which Authorization Server protects /mcp."""
    return {
        "resource": f"{base_url}/mcp",
        "authorization_servers": [base_url],
        "scopes_supported": [OAUTH_SCOPE],
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata(base_url: str) -> dict[str, Any]:
    """RFC 8414 — our own AS metadata (we issue the tokens)."""
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "registration_endpoint": f"{base_url}/register",
        "response_types_supported": ["code"],
        # Without "refresh_token" advertised here a client never even attempts a
        # refresh, and every token it holds dies for good after an hour.
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [OAUTH_SCOPE],
    }


def render_authorize_page(params: dict[str, str]) -> str:
    """Minimal one-click demo login + third-party consent screen.

    Real deployments would show a proper login (optionally "sign in with Kakao"
    as one method) and persist the consent decision. For the mock, clicking
    "로그인 후 동의" resolves the fixed demo user and mints an authorization code.
    """
    carried = (
        "response_type",
        "client_id",
        "redirect_uri",
        "state",
        "scope",
        "code_challenge",
        "code_challenge_method",
        "resource",
    )
    hidden = "\n".join(
        f'<input type="hidden" name="{escape(key)}" value="{escape(params.get(key, ""))}">'
        for key in carried
    )
    client_id = escape(params.get("client_id", "(unknown client)"))
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unit Expert 로그인</title>
  <style>
    body {{ margin:0; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      background:#f6f7f9; color:#17201d; display:flex; min-height:100vh; align-items:center; justify-content:center; }}
    .card {{ background:#fff; border:1px solid #d9e0dd; border-radius:12px; box-shadow:0 10px 30px rgba(20,34,30,.08);
      width:min(420px, calc(100vw - 32px)); padding:28px; }}
    h1 {{ font-size:20px; margin:0 0 4px; }}
    .sub {{ color:#62706b; font-size:13px; margin-bottom:18px; }}
    .client {{ font-size:12px; color:#62706b; background:#f1f5f4; border:1px solid #d9e0dd; border-radius:8px;
      padding:10px 12px; margin-bottom:16px; word-break:break-all; }}
    .consent {{ display:flex; gap:9px; align-items:flex-start; font-size:13px; line-height:1.5;
      background:#f1f5f4; border:1px solid #d9e0dd; border-radius:8px; padding:12px; margin-bottom:16px; }}
    .consent input {{ margin-top:3px; width:16px; height:16px; }}
    button {{ width:100%; border:0; border-radius:8px; padding:12px; font-size:14px; font-weight:700; cursor:pointer; }}
    .primary {{ background:#0f766e; color:#fff; }}
    .primary:hover {{ background:#115e59; }}
    .kakao {{ background:#fee500; color:#191600; margin-top:8px; }}
    .note {{ color:#8a5a00; font-size:11px; margin-top:12px; }}
  </style>
</head>
<body>
  <form class="card" method="POST" action="/authorize">
    {hidden}
    <h1>Unit Expert 로그인</h1>
    <div class="sub">아래 클라이언트가 접근 권한을 요청했습니다.</div>
    <div class="client"><b>client_id</b><br>{client_id}</div>
    <label class="consent">
      <input type="checkbox" name="consent" value="1" checked>
      <span>개인정보 제3자 제공에 동의합니다. (Unit Expert 계정 정보를 위 클라이언트에 제공)</span>
    </label>
    <button class="primary" type="submit" name="action" value="approve">로그인 후 동의하고 계속</button>
    <button class="kakao" type="button" disabled>카카오로 로그인 (데모에서는 비활성)</button>
    <p class="note">데모: 로그인은 고정 사용자(demo-user)로 처리됩니다. 카카오 로그인은 우리 로그인 계층의 한 옵션일 뿐, 인증서버 자체가 아닙니다.</p>
  </form>
</body>
</html>"""

SCENARIO_TITLES = {
    "ok": "정상 응답",
    "auth-401": "인증 조건 - 401 반환",
    "auth-403": "인증 조건 - 403 반환",
    "unsupported-min-version": "MCP 버전 조건 - 최소 지원 버전",
    "tools-list-error": "툴 목록 조건 - JSON-RPC 에러",
    "tools-list-null": "툴 목록 조건 - null 반환",
    "tools-list-empty": "툴 목록 조건 - 빈 배열 반환",
    "duplicate-tool-name": "툴 이름 조건 - 중복되는 툴",
    "too-many-tools": "툴 개수 조건 - 최대 개수",
    "invalid-tool-name-char": "툴 이름 조건 - 허용되지 않는 문자 사용",
    "invalid-tool-name-length": "툴 이름 조건 - 길이 초과",
    "missing-name": "툴 필수 속성 - name 없음",
    "missing-description": "툴 필수 속성 - description 없음",
    "missing-input-schema": "툴 필수 속성 - inputSchema 없음",
    "missing-annotations": "툴 필수 속성 - annotations 없음",
    "forbidden-kakao-name": "툴 이름 조건 - 금지어 사용",
    "long-description": "툴 설명 조건 - 길이 초과",
    "missing-service-name-in-description": "툴 설명 조건 - 서비스명 미포함",
    "incomplete-annotations": "툴 annotations 조건 - 필수 힌트",
    "delayed-response": "응답속도 조건 - 지연 / timeout 발생",
}

SCENARIO_DESCRIPTIONS = {
    "ok": "정상 Unit Expert 툴 목록을 반환합니다.",
    "auth-401": "JSON-RPC 처리 전에 401 Unauthorized를 반환합니다.",
    "auth-403": "JSON-RPC 처리 전에 403 Forbidden을 반환합니다.",
    "unsupported-min-version": "최소 지원 버전보다 낮은 protocolVersion 2024-11-05를 반환합니다.",
    "tools-list-error": "tools/list에서 JSON-RPC error를 반환합니다.",
    "tools-list-null": "tools/list에서 tools: null을 반환합니다.",
    "tools-list-empty": "tools/list에서 tools: []를 반환합니다.",
    "duplicate-tool-name": "동일한 name을 가진 중복 tool을 반환합니다.",
    "too-many-tools": "툴 21개를 반환합니다.",
    "invalid-tool-name-char": "허용되지 않는 문자가 포함된 tool name을 반환합니다.",
    "invalid-tool-name-length": "129자 길이의 tool name을 반환합니다.",
    "missing-name": "name이 없는 tool을 반환합니다.",
    "missing-description": "description이 없는 tool을 반환합니다.",
    "missing-input-schema": "inputSchema가 없는 tool을 반환합니다.",
    "missing-annotations": "annotations가 없는 tool을 반환합니다.",
    "forbidden-kakao-name": "금지어가 포함된 tool name을 반환합니다.",
    "long-description": "1,051자 description을 반환합니다.",
    "missing-service-name-in-description": "서비스명이 빠진 description을 반환합니다.",
    "incomplete-annotations": "필수 필드가 빠진 annotations를 반환합니다.",
    "delayed-response": "OPTIONS가 아닌 /mcp 요청을 5초 지연시킵니다.",
}

SCENARIO_GROUPS = (
    ("기본", ("ok",)),
    (
        "서버 error",
        (
            "auth-401",
            "auth-403",
            "unsupported-min-version",
            "tools-list-error",
            "tools-list-null",
            "tools-list-empty",
            "duplicate-tool-name",
            "too-many-tools",
            "delayed-response",
        ),
    ),
    (
        "tool error",
        (
            "invalid-tool-name-char",
            "invalid-tool-name-length",
            "missing-name",
            "missing-description",
            "missing-input-schema",
            "missing-annotations",
            "forbidden-kakao-name",
            "long-description",
            "missing-service-name-in-description",
            "incomplete-annotations",
        ),
    ),
)

SERVER_ERROR_GROUPS = (
    (
        "인증/접근",
        "택 1",
        (
            "auth-401",
            "auth-403",
        ),
    ),
    (
        "MCP 스펙/응답",
        "복수 선택 가능",
        (
            "unsupported-min-version",
            "delayed-response",
        ),
    ),
    (
        "tools/list 응답",
        "택 1",
        (
            "tools-list-error",
            "tools-list-null",
            "tools-list-empty",
            "too-many-tools",
        ),
    ),
    (
        "tools/list 툴 구성",
        "정상 tools 반환일 때 선택 가능",
        (
            "duplicate-tool-name",
        ),
    ),
)

TOOL_ERROR_SCENARIOS = (
    "invalid-tool-name-char",
    "invalid-tool-name-length",
    "missing-name",
    "missing-description",
    "missing-input-schema",
    "missing-annotations",
    "forbidden-kakao-name",
    "long-description",
    "missing-service-name-in-description",
    "incomplete-annotations",
)

TOOLS_LIST_MODES = {
    "normal": "정상 tools 반환",
    "json-rpc-error": "JSON-RPC error 반환",
    "null": "tools: null 반환",
    "empty": "tools: [] 반환",
    "too-many": "너무 많은 tools 반환",
}

# server/discover injection (modern path only). Applied to a discover request
# that passed envelope validation.
DISCOVER_MODES = {
    "normal": "정상 DiscoverResult",
    "no-modern-version": "supportedVersions에 2026-07-28 없음(2025 버전만)",
    "empty-versions": "supportedVersions: []",
    "missing-versions": "supportedVersions 필드 없음",
    "json-rpc-error": "HTTP 200 + JSON-RPC error(discover.errorCode)",
    "header-mismatch": "HTTP 400 + -32020",
    "unsupported-version": "HTTP 400 + -32022(data.supported/requested)",
    "http-401": "HTTP 401 + WWW-Authenticate",
    "http-403": "HTTP 403",
    "http-5xx": "HTTP 5xx(discover.httpStatus), 바디는 평문",
    "sse": "text/event-stream: 알림 1건 뒤 정상 응답",
    "sse-no-response": "text/event-stream: 알림만 보내고 응답 없이 종료",
    "id-mismatch": "요청과 다른 id의 정상 응답",
    "invalid-json": "HTTP 200 + JSON이 아닌 바디",
    "disconnect": "응답 없이 연결 종료",
}

# modernResponse.resultType: how the modern result's resultType is emitted.
MODERN_RESULT_TYPE_MODES = {
    "normal": "스펙대로(complete 등)",
    "omit": "resultType 필드 제거",
    "input_required": "resultType을 input_required로 바꿈(requestState 포함)",
    "unknown": "알 수 없는 값(bogus_result_type)",
}
MODERN_RESULT_METHODS = ("server/discover", "tools/list", "tools/call")
UNKNOWN_RESULT_TYPE = "bogus_result_type"

# legacyModernProbe.mode: what a legacy-only server (serverEra="legacy") answers
# to a request that carries 2026 markers or asks for server/discover.
LEGACY_MODERN_PROBE_MODES = {
    "method-not-found": "2025 방식으로 처리, server/discover는 HTTP 200 + -32601 (기본)",
    "invalid-params": "모르는 메서드에 HTTP 200 + -32602 (Python SDK 1.x 형태)",
    "http-400": "2025 버전이 아닌 MCP-Protocol-Version 헤더면 HTTP 400 + JSON-RPC -32600 (Python SDK 1.x 형태)",
    "http-404": "HTTP 404, JSON-RPC 바디 없음",
    "empty-400": "HTTP 400, 바디 없음",
    "timeout": "legacyModernProbe.delaySeconds 동안 무응답 후 연결 종료",
}
# Modes decided before the legacy Accept rule (they answer on the transport level).
LEGACY_MODERN_PROBE_TRANSPORT_MODES = ("http-400", "http-404", "empty-400", "timeout")

# toolCall.mode: tools/call injection shared by both eras.
TOOL_CALL_MODES = {
    "normal": "정상 실행",
    "is-error": "isError: true 결과",
    "json-rpc-error": "HTTP 200 + JSON-RPC error(toolCall.errorCode)",
    "http-status": "HTTP toolCall.httpStatus, 바디는 평문",
    "sse": "text/event-stream: 알림 toolCall.sseNotificationCount건 뒤 최종 응답",
    "id-mismatch": "요청과 다른 id의 정상 응답",
    "empty": "HTTP 200 + 빈 바디",
    "disconnect": "응답 없이 연결 종료",
}

# Client capabilities the server can be told to require (-32021).
CLIENT_CAPABILITY_NAMES = ("elicitation", "sampling", "roots")
MAX_INJECTED_DELAY_SECONDS = 120.0

DEFAULT_CONFIG: dict[str, Any] = {
    # "2025" -> initialize handshake + session id (legacy).
    # "2026" -> stateless per-request _meta, no handshake/session.
    "protocolEra": "2025",
    # True (default): the modern (2026-07-28) path follows the published spec —
    # required headers, -32022 data, base64 sentinel, Mcp-Param-*, 404 for unknown
    # or removed methods, -32602 for unknown tools, DiscoverResult shape, ...
    # False: the looser earlier behaviour, kept for comparison runs.
    # The legacy (handshake) path is not affected by this flag.
    "strict2026": True,
    # Origin allow-list for /mcp. Empty (default) = every Origin is accepted, so
    # the test server stays reachable from any page. Non-empty: a request whose
    # Origin header is not listed gets 403 (spec: Origin validation MUST).
    "allowedOrigins": [],
    # When True, the server refuses any request that is NOT on the 2026 transport
    # (i.e. legacy initialize-handshake clients) with -32022. Used to verify that
    # an older client which doesn't speak 2026-07-28 actually errors out against a
    # 2026-only server, instead of silently succeeding via the hospitable legacy path.
    "rejectLegacy": False,
    # Which protocol generations this server pretends to implement.
    #   "both"   -> serve 2026 (stateless) and legacy (handshake) requests. Default.
    #   "legacy" -> behave like a server that predates 2026-07-28: 2026 markers
    #               (Mcp-Method, _meta protocolVersion) are ignored, everything
    #               runs on the legacy path, and server/discover is an unknown
    #               method (-32601).
    #   "modern" -> 2026 only; same as rejectLegacy=True.
    # rejectLegacy is kept as a backward-compatible alias of "modern". When the
    # two disagree, an explicit serverEra of "legacy"/"modern" wins; rejectLegacy
    # only promotes the default "both" to "modern". normalize_config rewrites
    # rejectLegacy to (serverEra == "modern") so the pair is always consistent.
    "serverEra": "both",
    "mcp": {
        "identifier": SERVER_NAME,
        "serviceName": SERVICE_NAME,
    },
    "server": {
        "httpStatus": 200,
        "target": "all",
        "delayEnabled": False,
        "delaySeconds": DEFAULT_DELAY_SECONDS,
    },
    "customHeader": {
        "enabled": False,
        "name": "X-Mock-Auth",
        "value": "allow",
    },
    "initialize": {
        "protocolVersionEnabled": True,
        "protocolVersion": "2025-03-26",
    },
    "toolsList": {
        "mode": "normal",
        "tooManyCount": 21,
        "duplicateToolName": False,
        "cacheTtlMs": 0,
        "cacheScope": "private",
        # 0 = no pagination (default). N > 0: N tools per page + nextCursor,
        # both eras. An unknown cursor is -32602.
        "pageSize": 0,
        # 0 = off. N > 0: the Nth page (1-based) answers with a JSON-RPC error so
        # a client that already holds earlier pages sees a partial list.
        "failAtPage": 0,
    },
    # Modern (2026-07-28) path options.
    "modern": {
        # Expose the modern-only test tools (echo_header_params, confirm_action)
        # in tools/list. They are never listed on the legacy path.
        "testTools": True,
        # Capabilities every modern request except server/discover must declare
        # in _meta clientCapabilities, else 400 + -32021.
        "requiredClientCapabilities": [],
        # HTTP status for -32602 answers whose status the spec does not pin down
        # (unknown tool, invalid cursor, bad requestState). 200 or 400.
        "invalidParamsHttpStatus": 200,
        # Mcp-Param-* header present although the body has no value for it. The
        # spec table does not cover this case; True (default) treats it as a
        # header/body mismatch (-32020), False ignores the header.
        "rejectUnexpectedParamHeader": True,
    },
    "discover": {
        "mode": "normal",
        "errorCode": ERR_INTERNAL,
        "httpStatus": 503,
        "delaySeconds": 0.0,
        "cacheTtlMs": 0,
        "cacheScope": "private",
    },
    # Spec violations injected into otherwise valid modern results.
    "modernResponse": {
        "resultType": "normal",
        "omitServerInfo": False,
        "omitCacheFields": False,
        "issueSessionId": False,
        # Methods the violations apply to; empty = all of MODERN_RESULT_METHODS.
        "methods": [],
    },
    # Only read when serverEra == "legacy".
    "legacyModernProbe": {
        "mode": "method-not-found",
        "delaySeconds": 10.0,
    },
    # tools/call injection, both eras.
    "toolCall": {
        "mode": "normal",
        "errorCode": ERR_INTERNAL,
        "httpStatus": 500,
        "delaySeconds": 0.0,
        "targetTools": [],
        "sseNotificationCount": 2,
    },
    # Legacy session handling.
    "session": {
        # False (default) = sessions are issued but never checked.
        "validate": False,
        # False = initialize does not return Mcp-Session-Id (validation is then off).
        "issueId": True,
    },
    "toolErrors": [],
    # OAuth mock. This server acts as its OWN Authorization Server and issues its
    # OWN tokens (for its own user, scoped to the registered client) — it does NOT
    # delegate to an external IdP as the AS. Any external login (e.g. Kakao) would
    # only be one option inside our /authorize login layer, never the AS itself.
    #   off         -> no auth, everything public (default, unchanged behaviour).
    #   oauth       -> whole /mcp is a protected resource; every request needs a
    #                  valid bearer token, else 401 + WWW-Authenticate challenge.
    #   mixed-oauth -> initialize/tools/list stay public (anonymous clients work);
    #                  only tools/call for a protected tool needs a bearer token.
    "auth": {
        "mode": "off",
        # True: a valid token whose scope lacks "mcp" gets 403 insufficient_scope.
        "enforceScope": False,
        "protectedTools": [
            "convert_length",
            "convert_weight",
            "convert_temperature",
            "convert_area",
            "convert_volume",
        ],
    },
    # Developer-authored /mcp responses ("always answer this"). Each rule matches a
    # JSON-RPC method (exact name, or "*" as fallback) and bypasses every other
    # check (delay, customHeader, httpStatus, auth, rejectLegacy, 2026 validation).
    "rawResponses": [],
}

# Raw response rule limits.
RAW_RESPONSE_MAX_RULES = 20
RAW_RESPONSE_MAX_METHOD_LENGTH = 128
RAW_RESPONSE_MAX_BODY_LENGTH = 65536
RAW_RESPONSE_CONTENT_TYPES = ("json", "sse")
RAW_RESPONSE_ID_PLACEHOLDER = '"{{id}}"'
# RFC 7230 token characters (header field-name).
HTTP_TOKEN_CHARS = frozenset(
    "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)

# Protocol generations the server can pretend to implement (config "serverEra").
SERVER_ERAS = ("both", "legacy", "modern")
# X-MCP-Test-Scenario values that switch serverEra for a single request. They are
# header-only shortcuts, deliberately not listed in SCENARIO_DESCRIPTIONS.
SERVER_ERA_SCENARIOS = {
    "server-era-legacy": "legacy",
    "server-era-modern": "modern",
}

# OAuth mode identifiers, reused by config validation and the control-page UI.
AUTH_MODES = ("off", "oauth", "mixed-oauth")

LENGTH_FACTORS = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "km": 1000.0,
    "in": 0.0254,
    "ft": 0.3048,
    "yd": 0.9144,
    "mi": 1609.344,
}

WEIGHT_FACTORS = {
    "mg": 0.000001,
    "g": 0.001,
    "kg": 1.0,
    "t": 1000.0,
    "oz": 0.028349523125,
    "lb": 0.45359237,
}

AREA_FACTORS = {
    "mm2": 0.000001,
    "cm2": 0.0001,
    "m2": 1.0,
    "km2": 1_000_000.0,
    "in2": 0.00064516,
    "ft2": 0.09290304,
    "yd2": 0.83612736,
    "acre": 4046.8564224,
}

VOLUME_FACTORS = {
    "ml": 0.001,
    "l": 1.0,
    "m3": 1000.0,
    "in3": 0.016387064,
    "ft3": 28.316846592,
    "cup": 0.2365882365,
    "pt": 0.473176473,
    "qt": 0.946352946,
    "gal": 3.785411784,
    "floz": 0.0295735295625,
}

TEMPERATURE_UNITS = ("c", "f", "k")

ALIASES = {
    "millimeter": "mm",
    "millimeters": "mm",
    "centimeter": "cm",
    "centimeters": "cm",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "kilometer": "km",
    "kilometers": "km",
    "kilometre": "km",
    "kilometres": "km",
    "inch": "in",
    "inches": "in",
    "foot": "ft",
    "feet": "ft",
    "yard": "yd",
    "yards": "yd",
    "mile": "mi",
    "miles": "mi",
    "milligram": "mg",
    "milligrams": "mg",
    "gram": "g",
    "grams": "g",
    "kilogram": "kg",
    "kilograms": "kg",
    "ton": "t",
    "tons": "t",
    "tonne": "t",
    "tonnes": "t",
    "ounce": "oz",
    "ounces": "oz",
    "pound": "lb",
    "pounds": "lb",
    "celsius": "c",
    "centigrade": "c",
    "fahrenheit": "f",
    "kelvin": "k",
    "sqmm": "mm2",
    "squaremillimeter": "mm2",
    "squaremillimeters": "mm2",
    "sqcm": "cm2",
    "squarecentimeter": "cm2",
    "squarecentimeters": "cm2",
    "sqm": "m2",
    "squaremeter": "m2",
    "squaremeters": "m2",
    "squaremetre": "m2",
    "squaremetres": "m2",
    "sqkm": "km2",
    "squarekilometer": "km2",
    "squarekilometers": "km2",
    "sqin": "in2",
    "squareinch": "in2",
    "squareinches": "in2",
    "sqft": "ft2",
    "squarefoot": "ft2",
    "squarefeet": "ft2",
    "sqyd": "yd2",
    "squareyard": "yd2",
    "squareyards": "yd2",
    "acres": "acre",
    "milliliter": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "cubicmeter": "m3",
    "cubicmeters": "m3",
    "cubicmetre": "m3",
    "cubicmetres": "m3",
    "cubicinch": "in3",
    "cubicinches": "in3",
    "cubicfoot": "ft3",
    "cubicfeet": "ft3",
    "cups": "cup",
    "pint": "pt",
    "pints": "pt",
    "quart": "qt",
    "quarts": "qt",
    "gallon": "gal",
    "gallons": "gal",
    "fluidounce": "floz",
    "fluidounces": "floz",
}

SUPPORTED_UNITS = {
    "length": tuple(LENGTH_FACTORS),
    "weight": tuple(WEIGHT_FACTORS),
    "temperature": TEMPERATURE_UNITS,
    "area": tuple(AREA_FACTORS),
    "volume": tuple(VOLUME_FACTORS),
}


def tools(service_name: str = SERVICE_NAME) -> list[dict[str, Any]]:
    value_unit_schema = {
        "type": "object",
        "properties": {
            "value": {"type": "number", "description": "Numeric value to convert."},
            "from_unit": {"type": "string", "description": "Source unit."},
            "to_unit": {"type": "string", "description": "Target unit."},
        },
        "required": ["value", "from_unit", "to_unit"],
        "additionalProperties": False,
    }
    return [
        {
            "name": "convert_length",
            "description": (
                f"{service_name} converts length values between mm, cm, m, km, in, ft, yd, "
                "and mi."
            ),
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Length"),
        },
        {
            "name": "convert_weight",
            "description": f"{service_name} converts weight values between mg, g, kg, t, oz, and lb.",
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Weight"),
        },
        {
            "name": "convert_temperature",
            "description": f"{service_name} converts temperature values between c, f, and k.",
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Temperature"),
        },
        {
            "name": "convert_area",
            "description": (
                f"{service_name} converts area values between mm2, cm2, m2, km2, in2, ft2, "
                "yd2, and acre."
            ),
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Area"),
        },
        {
            "name": "convert_volume",
            "description": (
                f"{service_name} converts volume values between ml, l, m3, in3, ft3, cup, "
                "pt, qt, gal, and floz."
            ),
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Volume"),
        },
        {
            "name": "list_supported_units",
            "description": f"{service_name} lists supported canonical units by conversion category.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "annotations": annotations("List Supported Units"),
        },
    ]


def modern_test_tools(service_name: str = SERVICE_NAME) -> list[dict[str, Any]]:
    """Test-only tools listed on the modern (2026-07-28) path only.

    They exercise two features that do not exist before 2026-07-28: arguments
    mirrored into ``Mcp-Param-*`` headers (``x-mcp-header``) and the
    input_required (MRTR) round trip. The legacy tools/list is left untouched.
    """
    return [
        {
            "name": HEADER_PARAMS_TOOL,
            "description": (
                f"{service_name} test tool that echoes its arguments. region, greeting, "
                "count and verbose are mirrored into Mcp-Param-* headers."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "description": "Plain ASCII string mirrored into Mcp-Param-Region.",
                        X_MCP_HEADER: "Region",
                    },
                    "greeting": {
                        "type": "string",
                        "description": (
                            "Free text (non-ASCII allowed) mirrored into Mcp-Param-Greeting; "
                            "non-ASCII values travel as =?base64?...?=."
                        ),
                        X_MCP_HEADER: "Greeting",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Integer mirrored into Mcp-Param-Count.",
                        X_MCP_HEADER: "Count",
                    },
                    "verbose": {
                        "type": "boolean",
                        "description": "Boolean mirrored into Mcp-Param-Verbose.",
                        X_MCP_HEADER: "Verbose",
                    },
                    "note": {
                        "type": "string",
                        "description": "Free text that is NOT mirrored into a header.",
                    },
                },
                "additionalProperties": False,
            },
            "annotations": annotations("Echo Header Params"),
        },
        {
            "name": MRTR_TOOL,
            "description": (
                f"{service_name} test tool for the input_required flow. The first call "
                "returns an elicitation request; the retry with inputResponses completes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "Label of the action to confirm."},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            "annotations": annotations("Confirm Action"),
        },
    ]


def annotations(title: str) -> dict[str, bool | str]:
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
        "idempotentHint": True,
    }


def normalize_scenario(raw_scenario: str | None) -> str:
    if not raw_scenario:
        return "ok"
    return raw_scenario.strip().lower() or "ok"


def default_config() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _merge_config_patch(config: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            _merge_config_patch(config[key], value)
        else:
            config[key] = json.loads(json.dumps(value))


def header_scenario_patches() -> dict[str, dict[str, Any]]:
    """X-MCP-Test-Scenario names that are header-only shortcuts for config patches.

    Deliberately not listed in SCENARIO_DESCRIPTIONS (same as SERVER_ERA_SCENARIOS).
    """
    patches: dict[str, dict[str, Any]] = {}
    for mode in DISCOVER_MODES:
        if mode != "normal":
            patches[f"discover-{mode}"] = {"discover": {"mode": mode}}
    for mode in TOOL_CALL_MODES:
        if mode != "normal":
            patches[f"tool-call-{mode}"] = {"toolCall": {"mode": mode}}
    for mode in LEGACY_MODERN_PROBE_MODES:
        patches[f"legacy-probe-{mode}"] = {
            "serverEra": "legacy",
            "legacyModernProbe": {"mode": mode},
        }
    for mode in MODERN_RESULT_TYPE_MODES:
        if mode != "normal":
            patches[f"modern-result-type-{mode.replace('_', '-')}"] = {
                "modernResponse": {"resultType": mode}
            }
    patches["modern-omit-server-info"] = {"modernResponse": {"omitServerInfo": True}}
    patches["modern-omit-cache-fields"] = {"modernResponse": {"omitCacheFields": True}}
    patches["modern-issue-session-id"] = {"modernResponse": {"issueSessionId": True}}
    patches["lenient-2026"] = {"strict2026": False}
    patches["session-validate"] = {"session": {"validate": True}}
    patches["session-no-id"] = {"session": {"issueId": False}}
    patches["tools-list-paged"] = {"toolsList": {"pageSize": 2}}
    for name in CLIENT_CAPABILITY_NAMES:
        patches[f"require-capability-{name}"] = {"modern": {"requiredClientCapabilities": [name]}}
    return patches


HEADER_SCENARIO_INITIALIZE_VERSION_PREFIX = "initialize-version-"
HEADER_SCENARIO_CONFIG_PREFIX = "cfg:"


def _apply_scenario(config: dict[str, Any], scenario: str) -> None:
    if scenario == "auth-401":
        config["server"]["httpStatus"] = 401
    elif scenario == "auth-403":
        config["server"]["httpStatus"] = 403
    elif scenario == "delayed-response":
        config["server"]["delayEnabled"] = True
    elif scenario == "unsupported-min-version":
        config["initialize"]["protocolVersionEnabled"] = True
        config["initialize"]["protocolVersion"] = "2024-11-05"
    elif scenario == "tools-list-error":
        config["toolsList"]["mode"] = "json-rpc-error"
    elif scenario == "tools-list-null":
        config["toolsList"]["mode"] = "null"
    elif scenario == "tools-list-empty":
        config["toolsList"]["mode"] = "empty"
    elif scenario == "too-many-tools":
        config["toolsList"]["mode"] = "too-many"
    elif scenario == "duplicate-tool-name":
        config["toolsList"]["duplicateToolName"] = True
    elif scenario in TOOL_ERROR_SCENARIOS:
        if scenario not in config["toolErrors"]:
            config["toolErrors"].append(scenario)
    elif scenario in SERVER_ERA_SCENARIOS:
        config["serverEra"] = SERVER_ERA_SCENARIOS[scenario]
        config["rejectLegacy"] = config["serverEra"] == "modern"
    elif scenario.startswith(HEADER_SCENARIO_INITIALIZE_VERSION_PREFIX):
        version = scenario[len(HEADER_SCENARIO_INITIALIZE_VERSION_PREFIX):]
        if version:
            config["initialize"]["protocolVersionEnabled"] = True
            config["initialize"]["protocolVersion"] = version
    else:
        patch = header_scenario_patches().get(scenario)
        if patch is not None:
            _merge_config_patch(config, patch)
            config["rejectLegacy"] = config["serverEra"] == "modern"


def scenario_to_config(raw_scenario: str | None) -> dict[str, Any]:
    """Config for an X-MCP-Test-Scenario value.

    One scenario name, or several joined with commas (applied left to right), or
    ``cfg:<token>`` carrying a full config token (same encoding as the ``cfg``
    query parameter). Unknown names are ignored, as before.
    """
    raw_text = (raw_scenario or "").strip()
    if raw_text.lower().startswith(HEADER_SCENARIO_CONFIG_PREFIX):
        return decode_config_token(raw_text[len(HEADER_SCENARIO_CONFIG_PREFIX):])
    config = default_config()
    for part in raw_text.split(","):
        scenario = normalize_scenario(part)
        if scenario != "ok":
            _apply_scenario(config, scenario)
    return config


def long_description(service_name: str = SERVICE_NAME) -> str:
    prefix = f"{service_name} "
    return prefix + ("a" * (1051 - len(prefix)))


def normalize_config(raw_config: Any) -> dict[str, Any]:
    if not isinstance(raw_config, dict):
        return default_config()

    config = default_config()

    protocol_era = str(raw_config.get("protocolEra", config["protocolEra"])).strip()
    if protocol_era not in {"2025", "2026"}:
        raise ValueError("protocolEra must be 2025 or 2026")
    config["protocolEra"] = protocol_era

    config["strict2026"] = _config_bool(raw_config, "strict2026", config["strict2026"], "strict2026")
    config["allowedOrigins"] = _config_str_list(
        raw_config.get("allowedOrigins", []), "allowedOrigins", max_items=50, max_length=512
    )

    config["rejectLegacy"] = bool(raw_config.get("rejectLegacy", config["rejectLegacy"]))

    server_era = str(raw_config.get("serverEra", config["serverEra"])).strip()
    if server_era not in SERVER_ERAS:
        raise ValueError("serverEra must be both, legacy, or modern")
    # rejectLegacy is the legacy alias of serverEra="modern": it only promotes the
    # default "both". An explicit "legacy"/"modern" wins over the alias.
    if server_era == "both" and config["rejectLegacy"]:
        server_era = "modern"
    config["serverEra"] = server_era
    config["rejectLegacy"] = server_era == "modern"

    mcp = raw_config.get("mcp") if isinstance(raw_config.get("mcp"), dict) else {}
    server = raw_config.get("server") if isinstance(raw_config.get("server"), dict) else {}
    initialize = raw_config.get("initialize") if isinstance(raw_config.get("initialize"), dict) else {}
    tools_list = raw_config.get("toolsList") if isinstance(raw_config.get("toolsList"), dict) else {}

    identifier = str(mcp.get("identifier", SERVER_NAME)).strip()
    if not identifier:
        raise ValueError("mcp.identifier must not be empty")
    if len(identifier) > 128:
        raise ValueError("mcp.identifier must be 128 characters or fewer")
    config["mcp"]["identifier"] = identifier

    service_name = str(mcp.get("serviceName", SERVICE_NAME)).strip()
    if not service_name:
        raise ValueError("mcp.serviceName must not be empty")
    if len(service_name) > 200:
        raise ValueError("mcp.serviceName must be 200 characters or fewer")
    config["mcp"]["serviceName"] = service_name

    http_status = int(server.get("httpStatus", 200))
    if http_status not in {200, 401, 403}:
        raise ValueError("server.httpStatus must be 200, 401, or 403")
    config["server"]["httpStatus"] = http_status

    target = str(server.get("target", config["server"]["target"]))
    if target not in {"initialize", "all"}:
        raise ValueError("server.target must be initialize or all")
    config["server"]["target"] = target

    config["server"]["delayEnabled"] = bool(server.get("delayEnabled", False))
    delay_seconds = float(server.get("delaySeconds", DEFAULT_DELAY_SECONDS))
    if delay_seconds < 0 or delay_seconds > 30:
        raise ValueError("server.delaySeconds must be between 0 and 30")
    config["server"]["delaySeconds"] = delay_seconds

    config["initialize"]["protocolVersionEnabled"] = bool(
        initialize.get(
            "protocolVersionEnabled",
            config["initialize"]["protocolVersionEnabled"],
        )
    )
    protocol_version = str(
        initialize.get("protocolVersion", config["initialize"]["protocolVersion"])
    ).strip()
    if not protocol_version:
        raise ValueError("initialize.protocolVersion must not be empty")
    config["initialize"]["protocolVersion"] = protocol_version

    mode = str(tools_list.get("mode", "normal"))
    if mode not in TOOLS_LIST_MODES:
        raise ValueError("toolsList.mode is invalid")
    config["toolsList"]["mode"] = mode

    too_many_count = int(tools_list.get("tooManyCount", 21))
    if too_many_count < 1 or too_many_count > 100:
        raise ValueError("toolsList.tooManyCount must be between 1 and 100")
    config["toolsList"]["tooManyCount"] = too_many_count
    config["toolsList"]["duplicateToolName"] = (
        mode in {"normal", "too-many"} and bool(tools_list.get("duplicateToolName", False))
    )
    cache_ttl_ms = int(tools_list.get("cacheTtlMs", config["toolsList"]["cacheTtlMs"]))
    if cache_ttl_ms < 0 or cache_ttl_ms > 3_600_000:
        raise ValueError("toolsList.cacheTtlMs must be between 0 and 3600000")
    cache_scope = str(tools_list.get("cacheScope", config["toolsList"]["cacheScope"])).strip()
    if cache_scope not in {"private", "public"}:
        raise ValueError("toolsList.cacheScope must be private or public")
    config["toolsList"]["cacheTtlMs"] = cache_ttl_ms
    config["toolsList"]["cacheScope"] = cache_scope
    config["toolsList"]["pageSize"] = _config_int(tools_list, "pageSize", 0, "toolsList.pageSize", 0, 100)
    config["toolsList"]["failAtPage"] = _config_int(
        tools_list, "failAtPage", 0, "toolsList.failAtPage", 0, 100
    )

    _normalize_injection_config(config, raw_config)

    raw_tool_errors = raw_config.get("toolErrors", [])
    if not isinstance(raw_tool_errors, list):
        raise ValueError("toolErrors must be a list")
    tool_errors = []
    for tool_error in raw_tool_errors:
        if tool_error not in TOOL_ERROR_SCENARIOS:
            raise ValueError(f"unknown tool error '{tool_error}'")
        if tool_error not in tool_errors:
            tool_errors.append(tool_error)
    config["toolErrors"] = tool_errors

    raw_custom_header = raw_config.get("customHeader")
    if isinstance(raw_custom_header, dict):
        config["customHeader"]["enabled"] = bool(raw_custom_header.get("enabled", False))

    raw_auth = raw_config.get("auth")
    if isinstance(raw_auth, dict):
        mode = str(raw_auth.get("mode", config["auth"]["mode"])).strip()
        if mode not in AUTH_MODES:
            raise ValueError(f"auth.mode must be one of {', '.join(AUTH_MODES)}")
        config["auth"]["mode"] = mode
        config["auth"]["enforceScope"] = _config_bool(
            raw_auth, "enforceScope", config["auth"]["enforceScope"], "auth.enforceScope"
        )
        raw_protected = raw_auth.get("protectedTools")
        if isinstance(raw_protected, list):
            protected = [name for name in raw_protected if isinstance(name, str)]
            config["auth"]["protectedTools"] = protected

    config["rawResponses"] = normalize_raw_responses(raw_config.get("rawResponses", []))

    return config


def _config_section(raw_config: dict[str, Any], key: str) -> dict[str, Any]:
    section = raw_config.get(key)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(f"{key} must be an object")
    return section


def _config_bool(section: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be a boolean")
    return value


def _config_int(
    section: dict[str, Any], key: str, default: int, where: str, low: int, high: int
) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{where} must be an integer between {low} and {high}")
    return value


def _config_seconds(section: dict[str, Any], key: str, default: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{where} must be a number")
    if value < 0 or value > MAX_INJECTED_DELAY_SECONDS:
        raise ValueError(f"{where} must be between 0 and {int(MAX_INJECTED_DELAY_SECONDS)}")
    return float(value)


def _config_choice(
    section: dict[str, Any], key: str, default: Any, where: str, choices: Any
) -> Any:
    value = section.get(key, default)
    if value not in tuple(choices):
        allowed = ", ".join(str(choice) for choice in choices)
        raise ValueError(f"{where} must be one of {allowed}")
    return value


def _config_str_list(
    value: Any,
    where: str,
    *,
    max_items: int,
    max_length: int,
    choices: Any = None,
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    if len(value) > max_items:
        raise ValueError(f"{where} must have {max_items} items or fewer")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{where} items must be non-empty strings")
        item = item.strip()
        if len(item) > max_length:
            raise ValueError(f"{where} items must be {max_length} characters or fewer")
        if choices is not None and item not in tuple(choices):
            allowed = ", ".join(choices)
            raise ValueError(f"{where} items must be one of {allowed}")
        if item not in result:
            result.append(item)
    return result


def _normalize_injection_config(config: dict[str, Any], raw_config: dict[str, Any]) -> None:
    """Validate the option groups (modern, discover, modernResponse, ...)."""
    modern = _config_section(raw_config, "modern")
    defaults = config["modern"]
    config["modern"] = {
        "testTools": _config_bool(modern, "testTools", defaults["testTools"], "modern.testTools"),
        "requiredClientCapabilities": _config_str_list(
            modern.get("requiredClientCapabilities", []),
            "modern.requiredClientCapabilities",
            max_items=len(CLIENT_CAPABILITY_NAMES),
            max_length=64,
            choices=CLIENT_CAPABILITY_NAMES,
        ),
        "invalidParamsHttpStatus": _config_choice(
            modern,
            "invalidParamsHttpStatus",
            defaults["invalidParamsHttpStatus"],
            "modern.invalidParamsHttpStatus",
            (200, 400),
        ),
        "rejectUnexpectedParamHeader": _config_bool(
            modern,
            "rejectUnexpectedParamHeader",
            defaults["rejectUnexpectedParamHeader"],
            "modern.rejectUnexpectedParamHeader",
        ),
    }

    discover = _config_section(raw_config, "discover")
    defaults = config["discover"]
    config["discover"] = {
        "mode": _config_choice(discover, "mode", defaults["mode"], "discover.mode", DISCOVER_MODES),
        "errorCode": _config_int(
            discover, "errorCode", defaults["errorCode"], "discover.errorCode", -32768, 32767
        ),
        "httpStatus": _config_int(
            discover, "httpStatus", defaults["httpStatus"], "discover.httpStatus", 500, 599
        ),
        "delaySeconds": _config_seconds(
            discover, "delaySeconds", defaults["delaySeconds"], "discover.delaySeconds"
        ),
        "cacheTtlMs": _config_int(
            discover, "cacheTtlMs", defaults["cacheTtlMs"], "discover.cacheTtlMs", 0, 86_400_000
        ),
        "cacheScope": _config_choice(
            discover, "cacheScope", defaults["cacheScope"], "discover.cacheScope", ("private", "public")
        ),
    }

    modern_response = _config_section(raw_config, "modernResponse")
    defaults = config["modernResponse"]
    config["modernResponse"] = {
        "resultType": _config_choice(
            modern_response,
            "resultType",
            defaults["resultType"],
            "modernResponse.resultType",
            MODERN_RESULT_TYPE_MODES,
        ),
        "omitServerInfo": _config_bool(
            modern_response, "omitServerInfo", defaults["omitServerInfo"], "modernResponse.omitServerInfo"
        ),
        "omitCacheFields": _config_bool(
            modern_response,
            "omitCacheFields",
            defaults["omitCacheFields"],
            "modernResponse.omitCacheFields",
        ),
        "issueSessionId": _config_bool(
            modern_response, "issueSessionId", defaults["issueSessionId"], "modernResponse.issueSessionId"
        ),
        "methods": _config_str_list(
            modern_response.get("methods", []),
            "modernResponse.methods",
            max_items=len(MODERN_RESULT_METHODS),
            max_length=64,
            choices=MODERN_RESULT_METHODS,
        ),
    }

    probe = _config_section(raw_config, "legacyModernProbe")
    defaults = config["legacyModernProbe"]
    config["legacyModernProbe"] = {
        "mode": _config_choice(
            probe, "mode", defaults["mode"], "legacyModernProbe.mode", LEGACY_MODERN_PROBE_MODES
        ),
        "delaySeconds": _config_seconds(
            probe, "delaySeconds", defaults["delaySeconds"], "legacyModernProbe.delaySeconds"
        ),
    }

    tool_call = _config_section(raw_config, "toolCall")
    defaults = config["toolCall"]
    config["toolCall"] = {
        "mode": _config_choice(tool_call, "mode", defaults["mode"], "toolCall.mode", TOOL_CALL_MODES),
        "errorCode": _config_int(
            tool_call, "errorCode", defaults["errorCode"], "toolCall.errorCode", -32768, 32767
        ),
        "httpStatus": _config_int(
            tool_call, "httpStatus", defaults["httpStatus"], "toolCall.httpStatus", 400, 599
        ),
        "delaySeconds": _config_seconds(
            tool_call, "delaySeconds", defaults["delaySeconds"], "toolCall.delaySeconds"
        ),
        "targetTools": _config_str_list(
            tool_call.get("targetTools", []), "toolCall.targetTools", max_items=50, max_length=256
        ),
        "sseNotificationCount": _config_int(
            tool_call,
            "sseNotificationCount",
            defaults["sseNotificationCount"],
            "toolCall.sseNotificationCount",
            0,
            20,
        ),
    }

    session = _config_section(raw_config, "session")
    defaults = config["session"]
    config["session"] = {
        "validate": _config_bool(session, "validate", defaults["validate"], "session.validate"),
        "issueId": _config_bool(session, "issueId", defaults["issueId"], "session.issueId"),
    }


def normalize_raw_responses(raw_rules: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_rules, list):
        raise ValueError("rawResponses must be a list")
    if len(raw_rules) > RAW_RESPONSE_MAX_RULES:
        raise ValueError(f"rawResponses must have {RAW_RESPONSE_MAX_RULES} rules or fewer")
    return [normalize_raw_response_rule(rule, index) for index, rule in enumerate(raw_rules)]


def normalize_raw_response_rule(raw_rule: Any, index: int) -> dict[str, Any]:
    where = f"rawResponses[{index}]"
    if not isinstance(raw_rule, dict):
        raise ValueError(f"{where} must be an object")

    enabled = raw_rule.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{where}.enabled must be a boolean")

    method = raw_rule.get("method")
    if not isinstance(method, str) or not method.strip():
        raise ValueError(f"{where}.method must not be empty")
    method = method.strip()
    if len(method) > RAW_RESPONSE_MAX_METHOD_LENGTH:
        raise ValueError(
            f"{where}.method must be {RAW_RESPONSE_MAX_METHOD_LENGTH} characters or fewer"
        )

    status = raw_rule.get("status", 200)
    if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
        raise ValueError(f"{where}.status must be an integer between 200 and 599")

    raw_headers = raw_rule.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise ValueError(f"{where}.headers must be an object")
    headers: dict[str, str] = {}
    for name, value in raw_headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError(f"{where}.headers names and values must be strings")
        if not name or any(char not in HTTP_TOKEN_CHARS for char in name):
            raise ValueError(f"{where}.headers name '{name}' is not a valid HTTP header name")
        if "\r" in value or "\n" in value:
            raise ValueError(f"{where}.headers['{name}'] must not contain CR or LF")
        headers[name] = value

    content_type = raw_rule.get("contentType", "json")
    if content_type not in RAW_RESPONSE_CONTENT_TYPES:
        raise ValueError(f"{where}.contentType must be json or sse")

    body = raw_rule.get("body", "")
    if not isinstance(body, str):
        raise ValueError(f"{where}.body must be a string")
    if len(body) > RAW_RESPONSE_MAX_BODY_LENGTH:
        raise ValueError(
            f"{where}.body must be {RAW_RESPONSE_MAX_BODY_LENGTH} characters or fewer"
        )

    return {
        "enabled": enabled,
        "method": method,
        "status": status,
        "headers": headers,
        "contentType": content_type,
        "body": body,
    }


def match_raw_response(config: dict[str, Any], payload: Any) -> dict[str, Any] | None:
    """Return the raw response rule for this JSON-RPC payload, if any.

    Exact method match wins over the "*" wildcard; ties go to list order.
    """
    if not isinstance(payload, dict):
        return None
    method = payload.get("method")
    if not isinstance(method, str):
        return None
    wildcard = None
    for rule in config.get("rawResponses") or []:
        if not rule.get("enabled", True):
            continue
        if rule.get("method") == method:
            return rule
        if wildcard is None and rule.get("method") == "*":
            wildcard = rule
    return wildcard


def render_raw_body(body: str, request_id: Any) -> str:
    """Substitute the quoted '"{{id}}"' placeholder with the JSON-encoded request id."""
    return body.replace(RAW_RESPONSE_ID_PLACEHOLDER, json.dumps(request_id, ensure_ascii=False))


def build_raw_response(rule: dict[str, Any], body_text: str) -> tuple[bytes, dict[str, str]]:
    """Wire body + ordered response headers (excluding CORS) for a raw response rule.

    Header order: Content-Type defaults, server-computed Content-Length, then the
    user's headers. A user Content-Type replaces the default; a user
    Content-Length is ignored so the framing always matches the body.
    """
    user_headers = {
        name: value
        for name, value in rule.get("headers", {}).items()
        if name.lower() != "content-length"
    }
    user_header_names = {name.lower() for name in user_headers}

    if rule["status"] in {204, 304}:
        body = b""
    elif rule["contentType"] == "sse":
        lines = body_text.splitlines() or [""]
        framed = "event: message\n" + "".join(f"data: {line}\n" for line in lines) + "\n"
        body = framed.encode("utf-8")
    else:
        body = body_text.encode("utf-8")

    headers: dict[str, str] = {}
    if "content-type" not in user_header_names:
        if rule["contentType"] == "sse":
            headers["Content-Type"] = "text/event-stream"
        else:
            headers["Content-Type"] = "application/json"
    if rule["contentType"] == "sse" and "cache-control" not in user_header_names:
        headers["Cache-Control"] = "no-store"
    headers["Content-Length"] = str(len(body))
    headers.update(user_headers)
    return body, headers


def get_active_config() -> dict[str, Any]:
    with SCENARIO_LOCK:
        return json.loads(json.dumps(ACTIVE_CONFIG or DEFAULT_CONFIG))


def set_active_config(raw_config: Any) -> dict[str, Any]:
    config = normalize_config(raw_config)
    global ACTIVE_CONFIG, ACTIVE_SCENARIO
    with SCENARIO_LOCK:
        ACTIVE_CONFIG = config
        ACTIVE_SCENARIO = "custom"
    return get_active_config()


def get_active_scenario() -> str:
    with SCENARIO_LOCK:
        return ACTIVE_SCENARIO


def set_active_scenario(raw_scenario: str | None) -> str:
    global ACTIVE_SCENARIO, ACTIVE_CONFIG

    scenario = normalize_scenario(raw_scenario)
    if scenario not in SCENARIO_DESCRIPTIONS:
        raise ValueError(f"unknown scenario '{scenario}'")

    with SCENARIO_LOCK:
        ACTIVE_SCENARIO = scenario
        ACTIVE_CONFIG = scenario_to_config(scenario)
    return scenario


def resolve_scenario(raw_header_scenario: str | None) -> str:
    if raw_header_scenario is not None and raw_header_scenario.strip():
        return normalize_scenario(raw_header_scenario)
    return get_active_scenario()


def resolve_config(raw_header_scenario: str | None) -> dict[str, Any]:
    if raw_header_scenario is not None and raw_header_scenario.strip():
        return scenario_to_config(raw_header_scenario)
    return get_active_config()


def encode_config_token(raw_config: Any) -> str:
    config = compact_config(normalize_config(raw_config))
    if not config:
        return ""
    body = json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def compact_config(config: dict[str, Any]) -> dict[str, Any]:
    default = default_config()

    def diff(value: Any, default_value: Any) -> Any:
        if isinstance(value, dict) and isinstance(default_value, dict):
            result = {
                key: diff(child_value, default_value.get(key))
                for key, child_value in value.items()
                if diff(child_value, default_value.get(key)) is not None
            }
            return result or None
        if value == default_value:
            return None
        return value

    return diff(config, default) or {}


def decode_config_token(raw_token: str) -> dict[str, Any]:
    token = raw_token.strip()
    if not token:
        raise ValueError("empty config token")
    padding = "=" * (-len(token) % 4)
    try:
        body = base64.urlsafe_b64decode(f"{token}{padding}".encode("ascii"))
        payload = json.loads(body.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid config token") from error
    return normalize_config(payload)


def valid_tool(
    name: str,
    description: str | None = None,
    service_name: str = SERVICE_NAME,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description or f"{service_name} converts common measurement units.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "number"},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        "annotations": annotations(name.replace("_", " ").title()),
    }


def mutated_tool_for_error(tool_error: str, service_name: str = SERVICE_NAME) -> list[dict[str, Any]]:
    if tool_error == "duplicate-tool-name":
        return [valid_tool("search_place", service_name=service_name), valid_tool("search_place", service_name=service_name)]
    if tool_error == "invalid-tool-name-char":
        return [valid_tool("search place!", service_name=service_name)]
    if tool_error == "invalid-tool-name-length":
        return [valid_tool("a" * 129, service_name=service_name)]
    if tool_error == "missing-name":
        tool = valid_tool("missing_name_case", service_name=service_name)
        tool.pop("name")
        return [tool]
    if tool_error == "missing-description":
        tool = valid_tool("missing_description_case", service_name=service_name)
        tool.pop("description")
        return [tool]
    if tool_error == "missing-input-schema":
        tool = valid_tool("missing_input_schema_case", service_name=service_name)
        tool.pop("inputSchema")
        return [tool]
    if tool_error == "missing-annotations":
        tool = valid_tool("missing_annotations_case", service_name=service_name)
        tool.pop("annotations")
        return [tool]
    if tool_error == "forbidden-kakao-name":
        return [valid_tool("kakao_search", service_name=service_name)]
    if tool_error == "long-description":
        return [
            valid_tool(
                "long_description_case",
                long_description(service_name),
                service_name=service_name,
            )
        ]
    if tool_error == "missing-service-name-in-description":
        return [valid_tool("missing_service_name_case", "Search places nearby.", service_name=service_name)]
    if tool_error == "incomplete-annotations":
        tool = valid_tool("incomplete_annotations_case", service_name=service_name)
        tool["annotations"] = {
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        return [tool]
    return []


def config_label(config: dict[str, Any], path: str) -> list[str]:
    """``["path=value"]`` when the option differs from its default, else ``[]``.

    Used for the ``injected`` list of the inspection log: an option is named
    there when the response would have been different with the default value.
    """
    value: Any = config
    default: Any = DEFAULT_CONFIG
    for key in path.split("."):
        value = value[key]
        default = default[key]
    if value == default:
        return []
    if isinstance(value, bool):
        return [f"{path}={'true' if value else 'false'}"]
    if isinstance(value, list):
        return [f"{path}={','.join(str(item) for item in value)}"]
    return [f"{path}={value}"]


def tools_list_labels(config: dict[str, Any], modern: bool, test_tools: bool = False) -> list[str]:
    """Options that shaped a tools/list answer, for the inspection log.

    ``modern`` adds the cache hints of a modern result, ``test_tools`` the
    switch that hides the modern test tools (strict modern path only).
    """
    tools_list = config["toolsList"]
    mode = tools_list["mode"]
    labels = config_label(config, "toolsList.mode")
    if mode == "json-rpc-error":
        return labels
    if mode == "too-many":
        labels += config_label(config, "toolsList.tooManyCount")
        if tools_list.get("duplicateToolName") and tools_list["tooManyCount"] >= 2:
            labels.append("toolsList.duplicateToolName")
    elif mode == "normal":
        if tools_list.get("duplicateToolName"):
            labels.append("toolsList.duplicateToolName")
        labels += config_label(config, "toolErrors")
        if test_tools and len(labels) == 0:
            labels += config_label(config, "modern.testTools")
    if modern:
        labels += config_label(config, "toolsList.cacheTtlMs")
        labels += config_label(config, "toolsList.cacheScope")
    return labels


def tools_for_config(config: dict[str, Any]) -> dict[str, Any] | None:
    tools_list = config["toolsList"]
    mode = tools_list["mode"]
    service_name = config["mcp"]["serviceName"]
    if mode == "null":
        return {"tools": None}
    if mode == "empty":
        return {"tools": []}
    if mode == "too-many":
        count = tools_list["tooManyCount"]
        generated_tools = [
            valid_tool(f"tool_{index}", service_name=service_name)
            for index in range(count)
        ]
        if tools_list.get("duplicateToolName") and count >= 2:
            generated_tools[1] = valid_tool("tool_0", service_name=service_name)
        return {"tools": generated_tools}
    if mode != "normal":
        return None

    configured_tools: list[dict[str, Any]] = []
    if tools_list.get("duplicateToolName"):
        configured_tools.extend(mutated_tool_for_error("duplicate-tool-name", service_name))
    for tool_error in config["toolErrors"]:
        configured_tools.extend(mutated_tool_for_error(tool_error, service_name))
    if configured_tools:
        return {"tools": configured_tools}
    return None


def request_meta(payload: Any) -> dict[str, Any]:
    """Extract params._meta as a dict (empty if absent/malformed)."""
    if not isinstance(payload, dict):
        return {}
    params = payload.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}


def request_era(payload: Any, headers: dict[str, str] | None = None) -> str:
    """Decide whether a request uses the 2026 (stateless) transport.

    The 2026 transport is identified by its *markers*, not by the version
    value: the ``Mcp-Method`` routing header, or a ``params._meta`` carrying the
    protocolVersion key. This is deliberate — a 2026 client asking for a version
    the server does not support must still be routed into the 2026 path so it
    gets -32022, rather than silently falling back to legacy. Legacy 2025 never
    sends ``Mcp-Method`` nor the ``_meta`` envelope, so it stays on the old path.

    ``MCP-Protocol-Version`` is deliberately NOT a marker. Every 2025 client
    echoes that header with the version it negotiated (and some advertise their
    own newest instead), so treating the value ``2026-07-28`` as a marker drags
    legacy-shaped requests onto a path whose ``_meta`` envelope they cannot
    satisfy — a 400 with no way back, on every request including ``initialize``.
    It could not serve the -32022 goal above either: it only matched the single
    version the server *does* support, so that branch was unreachable through
    it. To refuse legacy clients on purpose, use the ``rejectLegacy`` config flag.

    A version the client *declares* is still decisive in the other direction: if
    it names a version that exists only on the legacy transport, that client is a
    legacy client and no header may outvote it. ``Mcp-Method``/``Mcp-Name`` are
    not proof of 2026 on their own, because gateways attach them as their own
    multi-server routing hints — PlayMCP sends ``Mcp-Method: tools/call`` with
    ``Mcp-Name: <serverId>-<tool>``, a namespaced value that is not even the
    body's ``params.name``, while declaring ``MCP-Protocol-Version: 2025-06-18``
    and carrying an ``Mcp-Session-Id``. Reading those hints as 2026 markers sent
    a 2025 client into a path whose envelope it never had.
    """
    lowered = {name.lower(): value for name, value in (headers or {}).items()}
    if str(lowered.get("mcp-protocol-version", "")).strip() in LEGACY_PROTOCOL_VERSIONS:
        return "2025"
    if "mcp-method" in lowered:
        return "2026"
    if META_PROTOCOL_VERSION in request_meta(payload):
        return "2026"
    return "2025"


def config_server_era(config: dict[str, Any] | None) -> str:
    """Effective serverEra of a (normalized) config; "both" when unset."""
    if not config:
        return "both"
    server_era = config.get("serverEra", "both")
    if server_era == "both" and config.get("rejectLegacy"):
        return "modern"
    return server_era if server_era in SERVER_ERAS else "both"


def validate_2026_request(
    payload: Any,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Validate a 2026-07-28 request the earlier (lenient) way.

    Only used with strict2026=False; validate_modern_request is the
    spec-conformant check. Returns a JSON-RPC error dict if the request is
    malformed, else None. The HTTP layer maps any returned error to a 400.
    """
    headers = headers or {}
    request_id = payload.get("id") if isinstance(payload, dict) else None
    method = payload.get("method") if isinstance(payload, dict) else None
    meta = request_meta(payload)

    # Case-insensitive header lookup.
    lowered = {name.lower(): value for name, value in headers.items()}

    # Protocol version: header must match body _meta.
    header_version = str(lowered.get("mcp-protocol-version", "")).strip()
    meta_version = str(meta.get(META_PROTOCOL_VERSION, "")).strip()
    if not meta_version:
        return json_rpc_error(
            request_id, ERR_INVALID_PARAMS, f"Missing _meta '{META_PROTOCOL_VERSION}'"
        )
    if header_version and header_version != meta_version:
        return json_rpc_error(
            request_id,
            ERR_HEADER_MISMATCH,
            f"Header mismatch: MCP-Protocol-Version '{header_version}' "
            f"does not match body value '{meta_version}'",
        )
    if meta_version not in SUPPORTED_PROTOCOL_VERSIONS:
        return json_rpc_error(
            request_id,
            ERR_UNSUPPORTED_VERSION,
            f"Unsupported protocol version '{meta_version}'. "
            f"Supported: {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}",
        )

    # clientCapabilities is a required envelope field. If the field itself is
    # missing, the request is malformed; -32021 is reserved for a present
    # declaration that lacks a capability required by the server.
    if META_CLIENT_CAPABILITIES not in meta:
        return json_rpc_error(
            request_id,
            ERR_INVALID_PARAMS,
            f"Missing _meta '{META_CLIENT_CAPABILITIES}'",
        )

    # Mcp-Method header is required and must match body method.
    header_method = lowered.get("mcp-method")
    if header_method is None:
        return json_rpc_error(request_id, ERR_HEADER_MISMATCH, "Missing Mcp-Method header")
    if header_method != method:
        return json_rpc_error(
            request_id,
            ERR_HEADER_MISMATCH,
            f"Header mismatch: Mcp-Method '{header_method}' does not match body '{method}'",
        )

    # Mcp-Name header is required for name-bearing methods and must match the body.
    if method in NAME_BEARING_METHODS:
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        body_name = params.get(NAME_BEARING_METHODS[method])
        header_name = lowered.get("mcp-name")
        if header_name is None:
            return json_rpc_error(
                request_id, ERR_HEADER_MISMATCH, f"Missing Mcp-Name header for {method}"
            )
        if header_name != body_name:
            return json_rpc_error(
                request_id,
                ERR_HEADER_MISMATCH,
                f"Header mismatch: Mcp-Name '{header_name}' does not match body '{body_name}'",
            )
    return None


def json_rpc_result_2026(
    request_id: Any,
    result: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a result in the 2026 envelope: resultType + _meta.serverInfo.

    The handler-provided result may set its own resultType (e.g. an error
    scenario that omits it); only fill defaults when absent. With a strict
    config the serverInfo name is the configured MCP identifier, so it agrees
    with what the legacy initialize result reports.
    """
    enriched = dict(result)
    enriched.setdefault("resultType", "complete")
    meta = dict(enriched.get("_meta") or {})
    server_name = SERVER_NAME
    if config is not None and config.get("strict2026", True):
        server_name = config["mcp"]["identifier"]
    meta.setdefault(META_SERVER_INFO, {"name": server_name, "version": SERVER_VERSION})
    enriched["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "result": enriched}


def validate_subscriptions_listen(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the 2026 subscriptions/listen request body."""
    request_id = payload.get("id")
    if "id" not in payload:
        return json_rpc_error(None, -32600, "subscriptions/listen must be a JSON-RPC request")

    params = payload.get("params")
    if not isinstance(params, dict):
        return json_rpc_error(request_id, ERR_INVALID_PARAMS, "Missing params")
    notifications = params.get("notifications")
    if not isinstance(notifications, dict):
        return json_rpc_error(request_id, ERR_INVALID_PARAMS, "Missing params.notifications")
    return None


def accepted_subscription_notifications(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of requested notification streams this server supports."""
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    requested = params.get("notifications") if isinstance(params.get("notifications"), dict) else {}
    accepted: dict[str, Any] = {}
    if requested.get("toolsListChanged") is True:
        accepted["toolsListChanged"] = True
    return accepted


def subscriptions_acknowledged_notification(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = payload.get("id")
    return {
        "jsonrpc": "2.0",
        "method": SUBSCRIPTIONS_ACKNOWLEDGED_METHOD,
        "params": {
            "notifications": accepted_subscription_notifications(payload),
            "_meta": {META_SUBSCRIPTION_ID: request_id},
        },
    }


def subscriptions_listen_result(
    request_id: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    return json_rpc_result_2026(
        request_id, {"_meta": {META_SUBSCRIPTION_ID: request_id}}, config
    )


# ---------------------------------------------------------------------------
# Reply plumbing. handle_mcp_exchange() returns a reply dict so that injected
# transport behaviour (SSE, delay, dropped connection, plain-text status) can be
# expressed; handle_json_rpc() is the older tuple view of the same thing.
# ---------------------------------------------------------------------------
def _reply(
    status: int = 200,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    *,
    transport: str = "json",
    events: list[dict[str, Any]] | None = None,
    text: str = "",
    content_type: str = "text/plain;charset=UTF-8",
    delay: float = 0.0,
    injected: list[str] | None = None,
) -> dict[str, Any]:
    """transport: json | sse | text | disconnect | auth-challenge."""
    return {
        "status": status,
        "body": body,
        "headers": dict(headers or {}),
        "transport": transport,
        "events": list(events or []),
        "text": text,
        "contentType": content_type,
        "delay": delay,
        "injected": list(injected or []),
    }


class RequestHeaders(dict):
    """Request headers plus the lower-cased names that arrived more than once.

    A plain mapping folds repeated header lines into one entry, so the raw
    carrier (``Handler.exchange_headers``) records the repeats here.
    """

    duplicated: frozenset[str] = frozenset()


def _lowered_headers(headers: Any) -> RequestHeaders:
    """Lower-case the header names; keeps (and extends) the duplicate record."""
    lowered = RequestHeaders()
    duplicated = set(getattr(headers, "duplicated", ()))
    for name, value in dict(headers or {}).items():
        key = str(name).lower()
        if key in lowered:
            duplicated.add(key)
        lowered[key] = str(value)
    lowered.duplicated = frozenset(duplicated)
    return lowered


def strip_ows(value: str) -> str:
    """Drop optional whitespace (SP / HTAB) around a field value (RFC 9110 5.5)."""
    return value.strip(" \t")


def is_clean_header_value(value: str) -> bool:
    """RFC 9110 field-value characters: visible ASCII, space, horizontal tab."""
    return all(char == "\t" or " " <= char <= "~" for char in value)


def decode_mcp_header_value(value: str) -> str | None:
    """Decode an Mcp-Name / Mcp-Param-* header value.

    Returns the plain value, the decoded text for a ``=?base64?...?=`` sentinel,
    or None when the value has invalid characters or a malformed sentinel.

    The sentinel payload is decoded as strictly as the official Python SDK 2.3.0
    (``mcp.shared.inbound.decode_header_value``): standard alphabet only (the
    URL-safe ``-``/``_`` are rejected), ``=`` padding required, and only the
    canonical encoding is accepted (no non-zero trailing bits).
    """
    if not is_clean_header_value(value):
        return None
    minimum = len(BASE64_SENTINEL_PREFIX) + len(BASE64_SENTINEL_SUFFIX)
    if (
        len(value) >= minimum
        and value.startswith(BASE64_SENTINEL_PREFIX)
        and value.endswith(BASE64_SENTINEL_SUFFIX)
    ):
        encoded = value[len(BASE64_SENTINEL_PREFIX):-len(BASE64_SENTINEL_SUFFIX)]
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return None
        if base64.b64encode(raw).decode("ascii") != encoded:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return value


def header_param_properties(
    schema: Any, path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], str]]:
    """(argument path, header name) for every x-mcp-header reachable via properties."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    found: list[tuple[tuple[str, ...], str]] = []
    if not isinstance(properties, dict):
        return found
    for key, sub_schema in properties.items():
        if not isinstance(sub_schema, dict):
            continue
        header_name = sub_schema.get(X_MCP_HEADER)
        if isinstance(header_name, str) and header_name:
            found.append((path + (key,), header_name))
        found.extend(header_param_properties(sub_schema, path + (key,)))
    return found


def header_param_type(schema: Any, path: tuple[str, ...]) -> Any:
    """The ``type`` keyword of the property at a ``properties``-key path."""
    node = schema
    for key in path:
        properties = node.get("properties") if isinstance(node, dict) else None
        node = properties.get(key) if isinstance(properties, dict) else None
    return node.get("type") if isinstance(node, dict) else None


# Header values eligible for the numeric comparison. Same gate as the official
# Python SDK 2.3.0 (mcp.shared.inbound._CANONICAL_DECIMAL): an optional minus,
# digits, an optional fraction. No exponent, no plus sign, no whitespace.
_CANONICAL_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def header_matches_argument(decoded: str, value: Any, prop_type: Any = None) -> bool:
    """Compare a decoded Mcp-Param-* value with the body argument.

    Same strictness as the official Python SDK 2.3.0
    (``mcp.shared.inbound._mcp_param_value_matches``):

    - An ``integer`` property with an integral body value compares numerically
      when the header is a canonical decimal: ``42``, ``42.0`` and ``042`` match
      42; ``42.5`` does not. The comparison is exact (no float round-trip).
    - Everything else compares against the text a client mirrors into the
      header: ``true``/``false`` for a boolean, ``str(value)`` for a number, the
      string itself. So ``+42``, ``4.2e1`` and `` 42`` never match 42.
    """
    if (
        prop_type == "integer"
        and not isinstance(value, bool)
        and (isinstance(value, int) or (isinstance(value, float) and value.is_integer()))
        and _CANONICAL_DECIMAL.fullmatch(decoded) is not None
    ):
        whole, _, fraction = decoded.partition(".")
        if fraction and set(fraction) != {"0"}:
            return False
        try:
            return int(whole) == int(value)
        except ValueError:
            return False
    if isinstance(value, bool):
        return decoded == ("true" if value else "false")
    if isinstance(value, (str, int, float)):
        try:
            return decoded == str(value)
        except ValueError:
            return False
    return decoded == json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def validate_mcp_param_headers(
    request_id: Any,
    tool: dict[str, Any],
    arguments: dict[str, Any],
    lowered: dict[str, str],
    reject_unexpected: bool = True,
) -> dict[str, Any] | None:
    """Spec: basic/transports/streamable-http "Server Behavior for Custom Headers".

    Value present -> header required and must match after decoding. Value null or
    absent -> header not expected. The spec table has no row for a header that is
    present anyway; with ``reject_unexpected`` (modern.rejectUnexpectedParamHeader,
    default) it names a value the body does not have and is treated as a
    header/body mismatch, otherwise it is ignored. Any failure is -32020.

    A recognized header that arrives more than once is rejected whatever its
    values are (first-copy and last-copy readers would disagree), like the
    official Python SDK 2.3.0. Optional whitespace around the value is dropped
    before anything else, as HTTP requires.
    """
    duplicated = getattr(lowered, "duplicated", frozenset())
    input_schema = tool.get("inputSchema")
    for path, header_name in header_param_properties(input_schema):
        value: Any = arguments
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
            if value is None:
                break
        display = f"Mcp-Param-{header_name}"
        argument = ".".join(path)
        header_key = MCP_PARAM_HEADER_PREFIX + header_name.lower()
        header = lowered.get(header_key)
        if header is not None and header_key in duplicated:
            return json_rpc_error(
                request_id, ERR_HEADER_MISMATCH, f"{display} header appears more than once"
            )
        if header is None:
            if value is None:
                continue
            return json_rpc_error(
                request_id,
                ERR_HEADER_MISMATCH,
                f"Missing {display} header for argument '{argument}'",
            )
        decoded = decode_mcp_header_value(strip_ows(header))
        if decoded is None:
            return json_rpc_error(
                request_id,
                ERR_HEADER_MISMATCH,
                f"Invalid {display} header: invalid characters or malformed base64 sentinel",
            )
        if value is None:
            if not reject_unexpected:
                continue
            return json_rpc_error(
                request_id,
                ERR_HEADER_MISMATCH,
                f"Header mismatch: {display} header is present but argument "
                f"'{argument}' has no value in the body",
            )
        if not header_matches_argument(decoded, value, header_param_type(input_schema, path)):
            return json_rpc_error(
                request_id,
                ERR_HEADER_MISMATCH,
                f"Header mismatch: {display} header value '{decoded}' does not match "
                f"body value {json.dumps(value, ensure_ascii=False)}",
            )
    return None


def validate_modern_request(
    payload: dict[str, Any], lowered: dict[str, str]
) -> tuple[int, dict[str, Any]] | None:
    """Strict 2026-07-28 request validation. Returns (HTTP status, error) or None.

    The spec does not order these checks; the order follows the checklist in
    docs/SPEC_2026-07-28_NOTES.md: id shape, required headers, required _meta,
    version header vs _meta, supported version, Mcp-Method, Mcp-Name.
    """
    request_id = payload.get("id")
    method = payload.get("method")
    if not isinstance(request_id, str) and (
        isinstance(request_id, bool) or not isinstance(request_id, int)
    ):
        return 400, json_rpc_error(
            None, ERR_INVALID_REQUEST, "Invalid Request: id must be a string or an integer"
        )
    if not isinstance(method, str):
        return 400, json_rpc_error(
            request_id, ERR_INVALID_REQUEST, "Invalid Request: method must be a string"
        )

    def mismatch(message: str) -> tuple[int, dict[str, Any]]:
        return 400, json_rpc_error(request_id, ERR_HEADER_MISMATCH, message)

    # A routing header sent more than once is rejected whatever its values are:
    # first-copy and last-copy readers would disagree (same rule as the official
    # Python SDK 2.3.0, mcp.shared.inbound.find_duplicated_routing_header).
    duplicated = getattr(lowered, "duplicated", frozenset())
    for key, display in (
        ("mcp-protocol-version", "MCP-Protocol-Version"),
        ("mcp-method", "Mcp-Method"),
        ("mcp-name", "Mcp-Name"),
    ):
        if key in duplicated:
            return mismatch(f"{display} header appears more than once")

    header_version = lowered.get("mcp-protocol-version", "").strip()
    if not header_version:
        return mismatch("Missing MCP-Protocol-Version header")
    # Optional whitespace around a field value is not part of the value.
    header_method = lowered.get("mcp-method")
    if header_method is None:
        return mismatch("Missing Mcp-Method header")
    header_method = strip_ows(header_method)
    header_name = lowered.get("mcp-name")
    if header_name is not None:
        header_name = strip_ows(header_name)
    if method in NAME_BEARING_METHODS and header_name is None:
        return mismatch(f"Missing Mcp-Name header for {method}")

    meta = request_meta(payload)
    meta_version = meta.get(META_PROTOCOL_VERSION)
    if not isinstance(meta_version, str) or not meta_version.strip():
        return 400, json_rpc_error(
            request_id, ERR_INVALID_PARAMS, f"Missing _meta '{META_PROTOCOL_VERSION}'"
        )
    if not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict):
        return 400, json_rpc_error(
            request_id,
            ERR_INVALID_PARAMS,
            f"Missing _meta '{META_CLIENT_CAPABILITIES}' (must be an object)",
        )

    if header_version != meta_version:
        return mismatch(
            f"Header mismatch: MCP-Protocol-Version '{header_version}' "
            f"does not match body value '{meta_version}'"
        )
    if meta_version not in MODERN_PROTOCOL_VERSIONS:
        return 400, json_rpc_error(
            request_id,
            ERR_UNSUPPORTED_VERSION,
            f"Unsupported protocol version '{meta_version}'. "
            f"Supported: {', '.join(MODERN_PROTOCOL_VERSIONS)}",
            {"supported": list(MODERN_PROTOCOL_VERSIONS), "requested": meta_version},
        )

    if header_method != method:
        return mismatch(
            f"Header mismatch: Mcp-Method '{header_method}' does not match body '{method}'"
        )

    if method in NAME_BEARING_METHODS:
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        body_name = params.get(NAME_BEARING_METHODS[method])
        decoded_name = decode_mcp_header_value(header_name or "")
        if decoded_name is None:
            return mismatch("Invalid Mcp-Name header: invalid characters or malformed base64 sentinel")
        if decoded_name != body_name:
            return mismatch(
                f"Header mismatch: Mcp-Name header value '{decoded_name}' "
                f"does not match body value '{body_name}'"
            )
    return None


def modern_envelope_failure(
    payload: dict[str, Any], headers: Any, config: dict[str, Any]
) -> tuple[int, dict[str, Any]] | None:
    """Envelope validation for the modern path, honouring strict2026."""
    if config.get("strict2026", True):
        return validate_modern_request(payload, _lowered_headers(headers))
    error = validate_2026_request(payload, dict(headers or {}))
    return (400, error) if error is not None else None


def declared_legacy_version(payload: dict[str, Any], lowered: dict[str, str]) -> str:
    """Best-effort ``requested`` for -32022 sent to a legacy-shaped request.

    initialize params.protocolVersion, else the MCP-Protocol-Version header, else
    2025-03-26 — the version the spec lets a server assume when the header is absent.
    """
    params = payload.get("params")
    if payload.get("method") == "initialize" and isinstance(params, dict):
        requested = params.get("protocolVersion")
        if isinstance(requested, str) and requested:
            return requested
    return lowered.get("mcp-protocol-version", "").strip() or "2025-03-26"


def modern_tool_definitions(config: dict[str, Any]) -> list[dict[str, Any]]:
    service_name = config["mcp"]["serviceName"]
    definitions = tools(service_name)
    if config["modern"]["testTools"]:
        definitions.extend(modern_test_tools(service_name))
    return definitions


def _invalid_params_reply(
    request_id: Any, config: dict[str, Any], message: str
) -> dict[str, Any]:
    """-32602 whose HTTP status the spec leaves open (modern.invalidParamsHttpStatus)."""
    return _reply(
        config["modern"]["invalidParamsHttpStatus"],
        json_rpc_error(request_id, ERR_INVALID_PARAMS, message),
        injected=config_label(config, "modern.invalidParamsHttpStatus"),
    )


def encode_tools_cursor(offset: int) -> str:
    return _b64url_encode(json.dumps({"offset": offset}, separators=(",", ":")).encode("utf-8"))


def decode_tools_cursor(cursor: Any) -> int | None:
    if not isinstance(cursor, str) or not cursor:
        return None
    try:
        payload = json.loads(_b64url_decode(cursor).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    offset = payload.get("offset") if isinstance(payload, dict) else None
    if isinstance(offset, bool) or not isinstance(offset, int):
        return None
    return offset


def paginate_tools_result(
    result: dict[str, Any], config: dict[str, Any], params: Any, request_id: Any
) -> tuple[dict[str, Any], dict[str, Any] | None, list[str]]:
    """Apply toolsList.pageSize / failAtPage. Returns (result, error, injected labels)."""
    page_size = config["toolsList"]["pageSize"]
    all_tools = result.get("tools")
    if page_size <= 0 or not isinstance(all_tools, list):
        return result, None, []
    labels = [f"toolsList.pageSize={page_size}"]
    cursor = params.get("cursor") if isinstance(params, dict) else None
    offset = 0
    if cursor is not None:
        decoded = decode_tools_cursor(cursor)
        if decoded is None or decoded <= 0 or decoded >= len(all_tools):
            return result, json_rpc_error(request_id, ERR_INVALID_PARAMS, "Invalid cursor"), labels
        offset = decoded
    page_number = offset // page_size + 1
    fail_at = config["toolsList"]["failAtPage"]
    if fail_at and fail_at == page_number:
        labels.append(f"toolsList.failAtPage={fail_at}")
        return (
            result,
            json_rpc_error(
                request_id, ERR_INTERNAL, f"Injected tools/list failure at page {page_number}"
            ),
            labels,
        )
    page = dict(result)
    page["tools"] = all_tools[offset:offset + page_size]
    if offset + page_size < len(all_tools):
        page["nextCursor"] = encode_tools_cursor(offset + page_size)
    return page, None, labels


# -- input_required (MRTR) test tool ------------------------------------------
def _request_state_digest(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def issue_request_state(tool_name: str, arguments: dict[str, Any]) -> str:
    """Opaque, HMAC-protected requestState bound to the tool, its arguments and a TTL."""
    payload = {
        "tool": tool_name,
        "args": _request_state_digest(arguments),
        "exp": int(time.time()) + MRTR_STATE_TTL_SECONDS,
    }
    body = _b64url_encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(JWT_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(signature)}"


def verify_request_state(state: Any, tool_name: str, arguments: dict[str, Any]) -> bool:
    if not isinstance(state, str) or state.count(".") != 1:
        return False
    body, signature = state.split(".")
    try:
        expected = hmac.new(
            JWT_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(signature)):
            return False
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError):
        return False
    if not isinstance(payload, dict):
        return False
    exp = payload.get("exp")
    return (
        payload.get("tool") == tool_name
        and payload.get("args") == _request_state_digest(arguments)
        and isinstance(exp, int)
        and exp >= int(time.time())
    )


def confirm_action_reply(
    request_id: Any,
    params: dict[str, Any],
    arguments: dict[str, Any],
    meta: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """tools/call for the MRTR test tool (spec: basic/patterns/mrtr)."""
    action = arguments.get("action")
    if not isinstance(action, str) or not action.strip():
        return _reply(
            200,
            json_rpc_result_2026(request_id, tool_error("action must be a non-empty string."), config),
        )

    # The tool cannot run without elicitation, and an elicitation/create input
    # request MUST NOT be sent to a client that did not declare the capability.
    capabilities = meta.get(META_CLIENT_CAPABILITIES)
    if not isinstance(capabilities, dict) or not isinstance(capabilities.get("elicitation"), dict):
        return _reply(
            400,
            json_rpc_error(
                request_id,
                ERR_MISSING_CAPABILITY,
                f"Tool '{MRTR_TOOL}' requires the client capability 'elicitation'",
                {"requiredCapabilities": {"elicitation": {}}},
            ),
        )

    state = params.get("requestState")
    if state is not None:
        if not verify_request_state(state, MRTR_TOOL, arguments):
            return _invalid_params_reply(request_id, config, "Invalid or expired requestState")
        responses = params.get("inputResponses")
        answer = responses.get(MRTR_INPUT_KEY) if isinstance(responses, dict) else None
        decision = answer.get("action") if isinstance(answer, dict) else None
        text = None
        if decision == "accept":
            content = answer.get("content")
            if isinstance(content, dict) and isinstance(content.get("confirmed"), bool):
                text = (
                    f"Action '{action}' confirmed."
                    if content["confirmed"]
                    else f"Action '{action}' was not confirmed."
                )
        elif decision == "decline":
            text = f"Action '{action}' was declined."
        elif decision == "cancel":
            text = f"Action '{action}' was cancelled."
        if text is not None:
            return _reply(
                200,
                json_rpc_result_2026(
                    request_id,
                    {"content": [{"type": "text", "text": text}], "isError": False},
                    config,
                ),
            )
        # Missing or incomplete answer: ask again instead of failing (SHOULD).

    result = {
        "resultType": "input_required",
        "inputRequests": {
            MRTR_INPUT_KEY: {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": f"Confirm the action '{action}'?",
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
        "requestState": issue_request_state(MRTR_TOOL, arguments),
    }
    return _reply(200, json_rpc_result_2026(request_id, result, config))


def echo_header_params_result(arguments: dict[str, Any]) -> dict[str, Any]:
    expected_types: dict[str, type] = {
        "region": str,
        "greeting": str,
        "count": int,
        "verbose": bool,
        "note": str,
    }
    for name, value in arguments.items():
        expected = expected_types.get(name)
        if expected is None:
            return tool_error(f"unknown argument '{name}'.")
        if value is None:
            continue
        if expected is int and isinstance(value, bool):
            return tool_error("count must be an integer.")
        if expected is int and isinstance(value, float) and value.is_integer():
            continue
        if not isinstance(value, expected):
            return tool_error(f"{name} must be of type {expected.__name__}.")
    text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return {"content": [{"type": "text", "text": text}], "isError": False}


# -- tools/call injection (both eras) -----------------------------------------
def tool_call_plan(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    """The toolCall injection that applies to this tools/call, if any."""
    tool_call = config["toolCall"]
    mode = tool_call["mode"]
    delay = tool_call["delaySeconds"]
    if mode == "normal" and delay <= 0:
        return None
    params = payload.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    targets = tool_call["targetTools"]
    if targets and name not in targets:
        return None
    labels = []
    if mode != "normal":
        labels.append(f"toolCall.mode={mode}")
    if delay > 0:
        labels.append(f"toolCall.delaySeconds={delay:g}")
    return {"mode": mode, "delay": delay, "labels": labels}


def request_scoped_notifications(payload: dict[str, Any], count: int) -> list[dict[str, Any]]:
    """Notifications that may precede the final response on a request's SSE stream.

    notifications/progress when the request carries _meta.progressToken, else
    notifications/message.
    """
    token = request_meta(payload).get("progressToken")
    notifications: list[dict[str, Any]] = []
    for index in range(count):
        if token is not None:
            notifications.append(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": token,
                        "progress": index + 1,
                        "total": count,
                        "message": f"step {index + 1}/{count}",
                    },
                }
            )
        else:
            notifications.append(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {
                        "level": "info",
                        "logger": SERVER_NAME,
                        "data": f"step {index + 1}/{count}",
                    },
                }
            )
    return notifications


def mismatched_id(request_id: Any) -> Any:
    if isinstance(request_id, int) and not isinstance(request_id, bool):
        return request_id + 1_000_000
    return f"mismatch-{request_id}"


def apply_tool_call_plan(
    plan: dict[str, Any] | None,
    normal: dict[str, Any],
    payload: dict[str, Any],
    config: dict[str, Any],
    wrap_result: Any,
) -> dict[str, Any]:
    """Turn the normal tools/call reply into the injected one.

    The mode only replaces a call that would have produced a result. A protocol
    error (header mismatch, unknown tool on the modern path, missing capability)
    is returned as it is; the delay still applies.
    """
    if plan is None:
        return normal
    request_id = payload.get("id")
    mode = plan["mode"]
    tool_call = config["toolCall"]
    reply = normal
    labels = plan["labels"]
    if not isinstance(normal["body"], dict) or "result" not in normal["body"]:
        mode = "normal"
        labels = [label for label in labels if not label.startswith("toolCall.mode=")]
    if mode == "is-error":
        reply = _reply(200, wrap_result(request_id, tool_error("Injected tool execution error")))
    elif mode == "json-rpc-error":
        reply = _reply(
            200,
            json_rpc_error(request_id, tool_call["errorCode"], "Injected tools/call failure"),
        )
    elif mode == "http-status":
        status = tool_call["httpStatus"]
        reply = _reply(status, transport="text", text=f"Injected HTTP {status}")
    elif mode == "empty":
        reply = _reply(200, transport="text", text="", content_type="application/json")
    elif mode == "disconnect":
        reply = _reply(0, transport="disconnect")
    elif mode == "sse":
        events = request_scoped_notifications(payload, tool_call["sseNotificationCount"])
        reply = dict(normal)
        reply["transport"] = "sse"
        reply["events"] = events + [normal["body"]]
    elif mode == "id-mismatch":
        reply = dict(normal)
        reply["body"] = {**normal["body"], "id": mismatched_id(request_id)}
    reply["delay"] = reply["delay"] + plan["delay"]
    reply["injected"] = reply["injected"] + labels
    return reply


# -- legacy-only server facing a modern request --------------------------------
def legacy_modern_probe_reply(
    payload: Any, config: dict[str, Any], headers: Any
) -> dict[str, Any] | None:
    """Transport-level answer of a legacy-only server to a modern request.

    Only for legacyModernProbe modes in LEGACY_MODERN_PROBE_TRANSPORT_MODES and
    only when serverEra == "legacy". The JSON-RPC-level modes (method-not-found,
    invalid-params) are handled on the legacy dispatch path instead.
    """
    if config_server_era(config) != "legacy" or not isinstance(payload, dict):
        return None
    probe = config["legacyModernProbe"]
    mode = probe["mode"]
    if mode not in LEGACY_MODERN_PROBE_TRANSPORT_MODES:
        return None
    lowered = _lowered_headers(headers)
    method = payload.get("method")
    label = [f"legacyModernProbe.mode={mode}"]

    if mode == "http-400":
        # Python SDK 1.x: a non-initialize request whose MCP-Protocol-Version
        # header names a version the server does not know is refused with 400.
        version = lowered.get("mcp-protocol-version", "").strip()
        known_versions = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        if (
            not version
            or version in known_versions
            or version in LEGACY_PROTOCOL_VERSIONS
            or method == "initialize"
        ):
            return None
        supported = ", ".join(known_versions)
        return _reply(
            400,
            json_rpc_error(
                "server-error",
                ERR_INVALID_REQUEST,
                f"Bad Request: Unsupported protocol version: {version}. "
                f"Supported versions: {supported}",
            ),
            injected=label,
        )

    is_modern_request = (
        request_era(payload, dict(headers or {})) == "2026" or method == "server/discover"
    )
    if not is_modern_request:
        return None
    if mode == "http-404":
        return _reply(404, transport="text", text="Not Found", injected=label)
    if mode == "empty-400":
        return _reply(400, transport="text", text="", injected=label)
    return _reply(0, transport="disconnect", delay=probe["delaySeconds"], injected=label)


def legacy_session_failure(
    config: dict[str, Any], method: Any, lowered: dict[str, str]
) -> dict[str, Any] | None:
    """session.validate: 400 without a session id, 404 for an unknown/ended one."""
    session = config["session"]
    if not session["validate"] or not session["issueId"] or method == "initialize":
        return None
    failure = session_error(lowered.get("mcp-session-id"))
    if failure is None:
        return None
    return _reply(failure[0], failure[1], injected=["session.validate"])


def session_error(session_id: str | None) -> tuple[int, dict[str, Any]] | None:
    """(HTTP status, body) for a request whose session id is not usable, else None.

    Bodies are byte-for-byte those of the official Python SDK 1.27.2
    (scripts/sdk1_legacy_server.py, observed 2026-11-23): the id is the string
    "server-error" and the code is -32600 in all three cases.
    """
    if not session_id:
        status, message = 400, "Bad Request: Missing session ID"
    elif session_id in TERMINATED_SESSIONS:
        status, message = 404, "Not Found: Session has been terminated"
    elif session_id not in SESSIONS:
        status, message = 404, "Session not found"
    else:
        return None
    return status, json_rpc_error("server-error", ERR_INVALID_REQUEST, message)


def terminate_session(session_id: str) -> None:
    """DELETE /mcp: later requests with this id get the "terminated" 404."""
    SESSIONS.discard(session_id)
    TERMINATED_SESSIONS.add(session_id)


def expire_sessions() -> int:
    """Forget every issued session id; with session.validate the next request is 404.

    Ended sessions are forgotten too, so afterwards every old id is simply unknown.
    """
    count = len(SESSIONS)
    SESSIONS.clear()
    TERMINATED_SESSIONS.clear()
    return count


# -- modern (2026-07-28) strict path -------------------------------------------
def missing_client_capabilities(required: list[str], meta: dict[str, Any]) -> dict[str, Any]:
    capabilities = meta.get(META_CLIENT_CAPABILITIES)
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    return {name: {} for name in required if not isinstance(capabilities.get(name), dict)}


def modern_discover_reply(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    request_id = payload.get("id")
    discover = config["discover"]
    mode = discover["mode"]
    delay = discover["delaySeconds"]
    labels = []
    if mode != "normal":
        labels.append(f"discover.mode={mode}")
    if delay > 0:
        labels.append(f"discover.delaySeconds={delay:g}")

    result: dict[str, Any] = {
        "supportedVersions": list(MODERN_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {"listChanged": True}},
        "ttlMs": discover["cacheTtlMs"],
        "cacheScope": discover["cacheScope"],
    }
    if mode == "no-modern-version":
        result["supportedVersions"] = ["2025-03-26", "2025-06-18", "2025-11-25"]
    elif mode == "empty-versions":
        result["supportedVersions"] = []
    elif mode == "missing-versions":
        result.pop("supportedVersions")
    body = json_rpc_result_2026(request_id, result, config)

    if mode == "json-rpc-error":
        reply = _reply(
            200, json_rpc_error(request_id, discover["errorCode"], "Injected server/discover failure")
        )
    elif mode == "header-mismatch":
        reply = _reply(
            400,
            json_rpc_error(
                request_id, ERR_HEADER_MISMATCH, "Header mismatch: injected by discover.mode"
            ),
        )
    elif mode == "unsupported-version":
        requested = str(request_meta(payload).get(META_PROTOCOL_VERSION, ""))
        supported = ["2099-01-01"]
        reply = _reply(
            400,
            json_rpc_error(
                request_id,
                ERR_UNSUPPORTED_VERSION,
                f"Unsupported protocol version '{requested}'. Supported: {', '.join(supported)}",
                {"supported": supported, "requested": requested},
            ),
        )
    elif mode == "http-401":
        reply = _reply(401, transport="auth-challenge")
    elif mode == "http-403":
        reply = _reply(403, transport="text", text="Forbidden")
    elif mode == "http-5xx":
        status = discover["httpStatus"]
        reply = _reply(status, transport="text", text=f"Injected HTTP {status}")
    elif mode == "sse":
        reply = _reply(
            200, body, transport="sse", events=request_scoped_notifications(payload, 1) + [body]
        )
    elif mode == "sse-no-response":
        reply = _reply(200, transport="sse", events=request_scoped_notifications(payload, 1))
    elif mode == "id-mismatch":
        reply = _reply(200, {**body, "id": mismatched_id(request_id)})
    elif mode == "invalid-json":
        reply = _reply(
            200, transport="text", text='{"jsonrpc":"2.0","id":', content_type="application/json"
        )
    elif mode == "disconnect":
        reply = _reply(0, transport="disconnect")
    else:
        reply = _reply(200, body)
    if isinstance(reply["body"], dict) and "result" in reply["body"]:
        labels += config_label(config, "discover.cacheTtlMs")
        labels += config_label(config, "discover.cacheScope")
    reply["delay"] = reply["delay"] + delay
    reply["injected"] = reply["injected"] + labels
    return reply


def modern_tools_list_reply(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    request_id = payload.get("id")
    params = payload.get("params")
    tools_list = config["toolsList"]
    mode_labels = tools_list_labels(config, modern=True, test_tools=True)
    if tools_list["mode"] == "json-rpc-error":
        return _reply(
            200,
            json_rpc_error(request_id, ERR_INTERNAL, "Injected tools/list failure"),
            injected=mode_labels,
        )
    scenario_tools = tools_for_config(config)
    result = dict(scenario_tools or {"tools": modern_tool_definitions(config)})
    result, error, labels = paginate_tools_result(result, config, params, request_id)
    if error is not None:
        status = 200
        if error["error"]["code"] == ERR_INVALID_PARAMS:
            status = config["modern"]["invalidParamsHttpStatus"]
            labels = labels + config_label(config, "modern.invalidParamsHttpStatus")
        return _reply(status, error, injected=labels)
    cursor = params.get("cursor") if isinstance(params, dict) else None
    if cursor is not None and tools_list["pageSize"] <= 0:
        # This server issued no cursor, so any cursor is one it does not know.
        return _invalid_params_reply(request_id, config, "Invalid cursor")
    labels = mode_labels + labels
    # Cacheable-list hints are required fields on a complete ListToolsResult.
    result.setdefault("ttlMs", tools_list["cacheTtlMs"])
    result.setdefault("cacheScope", tools_list["cacheScope"])
    return _reply(200, json_rpc_result_2026(request_id, result, config), injected=labels)


def modern_tools_call_reply(
    payload: dict[str, Any], config: dict[str, Any], lowered: dict[str, str]
) -> dict[str, Any]:
    request_id = payload.get("id")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    name = params.get("name")
    if not isinstance(name, str):
        return _invalid_params_reply(request_id, config, "tools/call requires a string params.name")
    tool = next((tool for tool in modern_tool_definitions(config) if tool["name"] == name), None)
    if tool is None:
        # Protocol error, not a tool execution error (spec: server/tools#error-handling).
        reply = _invalid_params_reply(request_id, config, f"Unknown tool: {name}")
        if name in (HEADER_PARAMS_TOOL, MRTR_TOOL):
            reply["injected"] = config_label(config, "modern.testTools") + reply["injected"]
        return reply
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _invalid_params_reply(request_id, config, "tools/call arguments must be an object")

    error = validate_mcp_param_headers(
        request_id,
        tool,
        arguments,
        lowered,
        reject_unexpected=config["modern"]["rejectUnexpectedParamHeader"],
    )
    if error is not None:
        return _reply(400, error)
    # A header the default config would have refused was let through.
    labels: list[str] = []
    if (
        not config["modern"]["rejectUnexpectedParamHeader"]
        and validate_mcp_param_headers(request_id, tool, arguments, lowered) is not None
    ):
        labels = config_label(config, "modern.rejectUnexpectedParamHeader")

    if name == MRTR_TOOL:
        reply = confirm_action_reply(request_id, params, arguments, request_meta(payload), config)
        reply["injected"] = labels + reply["injected"]
        return reply
    if name == HEADER_PARAMS_TOOL:
        result = echo_header_params_result(arguments)
    else:
        result = call_tool(params)
    return _reply(200, json_rpc_result_2026(request_id, result, config), injected=labels)


def apply_modern_response_injection(
    reply: dict[str, Any], config: dict[str, Any], method: str
) -> dict[str, Any]:
    """modernResponse.*: break an otherwise valid modern result on purpose."""
    injection = config["modernResponse"]
    methods = injection["methods"] or list(MODERN_RESULT_METHODS)
    body = reply["body"]
    if method not in methods or not isinstance(body, dict) or "result" not in body:
        return reply
    result = dict(body["result"])
    labels: list[str] = []
    headers = dict(reply["headers"])

    result_type = injection["resultType"]
    if result_type == "omit":
        result.pop("resultType", None)
    elif result_type == "input_required":
        result["resultType"] = "input_required"
        result.setdefault("requestState", "injected-request-state")
    elif result_type == "unknown":
        result["resultType"] = UNKNOWN_RESULT_TYPE
    if result_type != "normal":
        labels.append(f"modernResponse.resultType={result_type}")

    if injection["omitServerInfo"]:
        meta = dict(result.get("_meta") or {})
        meta.pop(META_SERVER_INFO, None)
        if meta:
            result["_meta"] = meta
        else:
            result.pop("_meta", None)
        labels.append("modernResponse.omitServerInfo")
    if injection["omitCacheFields"] and ("ttlMs" in result or "cacheScope" in result):
        result.pop("ttlMs", None)
        result.pop("cacheScope", None)
        labels.append("modernResponse.omitCacheFields")
    if injection["issueSessionId"]:
        headers["Mcp-Session-Id"] = str(uuid.uuid4())
        labels.append("modernResponse.issueSessionId")
    if not labels:
        return reply

    new_body = {**body, "result": result}
    updated = dict(reply)
    updated["body"] = new_body
    updated["headers"] = headers
    if reply["transport"] == "sse" and reply["events"] and reply["events"][-1] is body:
        updated["events"] = reply["events"][:-1] + [new_body]
    updated["injected"] = reply["injected"] + labels
    return updated


def handle_modern_request(
    payload: dict[str, Any], config: dict[str, Any], headers: Any
) -> dict[str, Any]:
    """Dispatch a 2026-07-28 request the way the published spec describes.

    Stateless: Mcp-Session-Id is neither read nor issued.
    """
    request_id = payload.get("id")
    method = payload.get("method")
    lowered = _lowered_headers(headers)
    failure = validate_modern_request(payload, lowered)
    if failure is not None:
        return _reply(failure[0], failure[1])
    if method not in MODERN_SUPPORTED_METHODS:
        # Unknown methods and methods removed in 2026-07-28 (initialize, ping, ...).
        return _reply(
            404, json_rpc_error(request_id, ERR_METHOD_NOT_FOUND, f"Method not found: {method}")
        )

    if method == "server/discover":
        return apply_modern_response_injection(modern_discover_reply(payload, config), config, method)

    missing = missing_client_capabilities(
        config["modern"]["requiredClientCapabilities"], request_meta(payload)
    )
    if missing:
        return _reply(
            400,
            json_rpc_error(
                request_id,
                ERR_MISSING_CAPABILITY,
                f"Missing required client capability: {', '.join(missing)}",
                {"requiredCapabilities": missing},
            ),
            injected=["modern.requiredClientCapabilities"],
        )

    if method == "tools/list":
        return apply_modern_response_injection(modern_tools_list_reply(payload, config), config, method)

    if method == "tools/call":
        normal = modern_tools_call_reply(payload, config, lowered)
        reply = apply_tool_call_plan(
            tool_call_plan(payload, config),
            normal,
            payload,
            config,
            lambda rid, result: json_rpc_result_2026(rid, result, config),
        )
        return apply_modern_response_injection(reply, config, method)

    error = validate_subscriptions_listen(payload)
    if error is not None:
        return _reply(400, error)
    return _reply(200, subscriptions_listen_result(request_id, config))


def handle_legacy_request(
    payload: dict[str, Any],
    config: dict[str, Any],
    headers: Any,
    check_session: bool = True,
) -> dict[str, Any]:
    """The initialize-handshake (2025 and earlier) path."""
    request_id = payload.get("id")
    method = payload.get("method")
    is_notification = "id" not in payload

    if check_session:
        failure = legacy_session_failure(config, method, _lowered_headers(headers))
        if failure is not None:
            return failure

    if method == "initialize":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        requested_version = params.get("protocolVersion")
        # Fall back to the newest *legacy* version, never to 2026-07-28: reaching
        # this handler means the client performed an initialize handshake, which
        # the 2026 transport does not have. Answering "2026-07-28" would hand a
        # stateful client a version it cannot speak.
        protocol_version = (
            requested_version
            if requested_version in LEGACY_PROTOCOL_VERSIONS
            else LATEST_LEGACY_PROTOCOL_VERSION
        )
        if config["initialize"]["protocolVersionEnabled"]:
            protocol_version = config["initialize"]["protocolVersion"]
        capabilities: dict[str, Any] = {"tools": {"listChanged": True}}
        result = {
            "protocolVersion": protocol_version,
            "capabilities": capabilities,
            "serverInfo": {"name": config["mcp"]["identifier"], "version": SERVER_VERSION},
        }
        # Named when the answer carries another version than the default config's.
        labels = []
        if protocol_version != DEFAULT_CONFIG["initialize"]["protocolVersion"]:
            labels = (
                [f"initialize.protocolVersion={protocol_version}"]
                if config["initialize"]["protocolVersionEnabled"]
                else ["initialize.protocolVersionEnabled=false"]
            )
        if not config["session"]["issueId"]:
            return _reply(
                200, json_rpc_result(request_id, result), injected=labels + ["session.issueId=false"]
            )
        session_id = str(uuid.uuid4())
        SESSIONS.add(session_id)
        return _reply(
            200, json_rpc_result(request_id, result), {"Mcp-Session-Id": session_id}, injected=labels
        )

    if method == "notifications/initialized":
        return _reply(202)

    if method == "ping":
        if is_notification:
            return _reply(202)
        return _reply(200, json_rpc_result(request_id, {}))

    if method == "tools/list":
        mode_labels = tools_list_labels(config, modern=False)
        if config["toolsList"]["mode"] == "json-rpc-error":
            return _reply(
                200,
                json_rpc_error(request_id, ERR_INTERNAL, "Injected tools/list failure"),
                injected=mode_labels,
            )
        scenario_tools = tools_for_config(config)
        result = scenario_tools or {"tools": tools(config["mcp"]["serviceName"])}
        result, error, labels = paginate_tools_result(
            result, config, payload.get("params"), request_id
        )
        if error is not None:
            return _reply(200, error, injected=labels)
        return _reply(200, json_rpc_result(request_id, result), injected=mode_labels + labels)

    if method == "tools/call":
        normal = _reply(200, json_rpc_result(request_id, call_tool(payload.get("params"))))
        return apply_tool_call_plan(
            tool_call_plan(payload, config), normal, payload, config, json_rpc_result
        )

    if is_notification:
        return _reply(202)
    probe_mode = config["legacyModernProbe"]["mode"]
    if config_server_era(config) == "legacy" and probe_mode in ("invalid-params", "http-400"):
        # Python SDK 1.x: an unknown method fails request validation. Body as
        # observed on mcp 1.27.2 (scripts/sdk1_legacy_server.py). http-400 is the
        # SDK 1.x transport check; a request that passes it (no version header)
        # gets this same answer from that SDK.
        return _reply(
            200,
            json_rpc_error(request_id, ERR_INVALID_PARAMS, "Invalid request parameters", ""),
            injected=[f"legacyModernProbe.mode={probe_mode}"],
        )
    return _reply(200, json_rpc_error(request_id, ERR_METHOD_NOT_FOUND, f"Method not found: {method}"))


def handle_mcp_exchange(
    payload: Any,
    scenario: str = "ok",
    config: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Decide the reply for one /mcp POST body. See _reply for the shape."""
    scenario = normalize_scenario(scenario)
    config = normalize_config(config) if config is not None else scenario_to_config(scenario)
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        message = "Invalid Request"
        if isinstance(payload, list):
            message = "Invalid Request: JSON-RPC batch requests are not supported"
        return _reply(400, json_rpc_error(None, ERR_INVALID_REQUEST, message))

    request_id = payload.get("id")
    method = payload.get("method")
    is_notification = "id" not in payload
    server_era = config_server_era(config)
    strict = config["strict2026"]

    # Legacy-only mode: a server that predates 2026-07-28 does not know the 2026
    # markers. legacyModernProbe picks how it reacts to them; by default they are
    # ignored, every request takes the handshake path, and server/discover is
    # just an unknown method (-32601).
    if server_era == "legacy":
        probe = legacy_modern_probe_reply(payload, config, headers)
        if probe is not None:
            return probe

    # 2026-07-28: stateless, per-request metadata. Detect via header/_meta and,
    # for actual requests, validate the envelope before dispatching.
    is_2026 = server_era != "legacy" and request_era(payload, headers) == "2026"
    # A request whose _meta carries the modern protocolVersion key is a modern
    # request even when its MCP-Protocol-Version header names a legacy version
    # (legacy clients never send that key). It must reach the modern validation,
    # which answers the header/_meta disagreement with -32020.
    if strict and server_era != "legacy" and META_PROTOCOL_VERSION in request_meta(payload):
        is_2026 = True

    # 2026-only mode: a legacy (handshake) client that does not speak the 2026
    # transport is refused with -32022 instead of being served via the old path.
    # This is how we prove an older client actually breaks against a 2026 server.
    if config.get("rejectLegacy") and not is_2026 and not is_notification:
        data = None
        if strict:
            data = {
                "supported": list(MODERN_PROTOCOL_VERSIONS),
                "requested": declared_legacy_version(payload, _lowered_headers(headers)),
            }
        return _reply(
            400,
            json_rpc_error(
                request_id,
                ERR_UNSUPPORTED_VERSION,
                "This server only supports protocol version "
                f"{PROTOCOL_VERSION_2026}. Legacy initialize-handshake clients are "
                f"not accepted. Supported: {PROTOCOL_VERSION_2026}",
                data,
            ),
        )

    if is_2026 and not is_notification:
        if strict:
            return handle_modern_request(payload, config, headers)
        # strict2026=False: the earlier lenient behaviour, plus toolCall injection.
        error = validate_2026_request(payload, headers)
        if error is not None:
            return _reply(400, error, injected=["strict2026=false"])
        status, extra_headers, body = handle_json_rpc_2026(payload, config, request_id, method)
        labels = ["strict2026=false"]
        if method == "tools/list":
            labels += tools_list_labels(config, modern=True)
        reply = _reply(status, body, extra_headers, injected=labels)
        if method == "tools/call":
            reply = apply_tool_call_plan(
                tool_call_plan(payload, config), reply, payload, config, json_rpc_result_2026
            )
        return reply

    # A modern-marked notification lands here too; it has no session to check.
    return handle_legacy_request(payload, config, headers, check_session=not is_2026)


def handle_json_rpc(
    payload: Any,
    scenario: str = "ok",
    config: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    """(HTTP status, extra headers, JSON-RPC body) view of handle_mcp_exchange.

    Replies that are not a single JSON object (SSE, plain text, dropped
    connection) report their final JSON-RPC message, or None when there is none.
    """
    reply = handle_mcp_exchange(payload, scenario, config, headers)
    return reply["status"], reply["headers"], reply["body"]


def handle_json_rpc_2026(
    payload: dict[str, Any],
    config: dict[str, Any],
    request_id: Any,
    method: Any,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    """Dispatch a 2026-07-28 request the earlier (lenient) way.

    Only reached with strict2026=False; the spec-conformant dispatcher is
    handle_modern_request. Stateless: no initialize handshake, no session id.
    Results carry the 2026 envelope (resultType + _meta.serverInfo) via
    json_rpc_result_2026.
    """
    if method == "initialize":
        # 2026 has no handshake, but a client may still probe. Respond without a
        # session id, advertising the negotiated version.
        result = {
            "protocolVersion": PROTOCOL_VERSION_2026,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": config["mcp"]["identifier"], "version": SERVER_VERSION},
        }
        return 200, {}, json_rpc_result_2026(request_id, result)

    if method == "server/discover":
        # Modern (2026) connect-time probe — the stateless replacement for the
        # initialize handshake. Advertise supported versions + capabilities so
        # the SDK client can adopt() and proceed to tools/list, tools/call.
        result = {
            "supportedVersions": [PROTOCOL_VERSION_2026],
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": config["mcp"]["identifier"], "version": SERVER_VERSION},
        }
        return 200, {}, json_rpc_result_2026(request_id, result)

    if method == "ping":
        return 200, {}, json_rpc_result_2026(request_id, {})

    if method == "tools/list":
        if config["toolsList"]["mode"] == "json-rpc-error":
            return 200, {}, json_rpc_error(request_id, -32603, "Injected tools/list failure")
        scenario_tools = tools_for_config(config)
        result = dict(scenario_tools or {"tools": tools(config["mcp"]["serviceName"])})
        # 2026 cacheable-list hints are required fields on ListToolsResult.
        result.setdefault("ttlMs", config["toolsList"]["cacheTtlMs"])
        result.setdefault("cacheScope", config["toolsList"]["cacheScope"])
        return 200, {}, json_rpc_result_2026(request_id, result)

    if method == "tools/call":
        result = call_tool(payload.get("params"))
        return 200, {}, json_rpc_result_2026(request_id, result)

    if method == SUBSCRIPTIONS_LISTEN_METHOD:
        error = validate_subscriptions_listen(payload)
        if error is not None:
            return 400, {}, error
        return 200, {}, subscriptions_listen_result(request_id)

    return 200, {}, json_rpc_error(request_id, -32601, f"Method not found: {method}")


def call_tool(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return tool_error("tools/call requires a tool name.")

    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    try:
        if params["name"] == "convert_length":
            return tool_success(convert_with_factor(arguments, "length", LENGTH_FACTORS))
        if params["name"] == "convert_weight":
            return tool_success(convert_with_factor(arguments, "weight", WEIGHT_FACTORS))
        if params["name"] == "convert_temperature":
            return tool_success(convert_temperature(arguments))
        if params["name"] == "convert_area":
            return tool_success(convert_with_factor(arguments, "area", AREA_FACTORS))
        if params["name"] == "convert_volume":
            return tool_success(convert_with_factor(arguments, "volume", VOLUME_FACTORS))
        if params["name"] == "list_supported_units":
            text = "\n".join(
                f"{category}: {', '.join(units)}"
                for category, units in SUPPORTED_UNITS.items()
            )
            return {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }
        return tool_error(f"Unknown tool: {params['name']}")
    except ValueError as error:
        return tool_error(str(error))


def convert_with_factor(
    arguments: dict[str, Any],
    category: str,
    factors: dict[str, float],
) -> dict[str, Any]:
    value = validate_value(arguments.get("value"))
    from_unit = normalize_unit(arguments.get("from_unit"))
    to_unit = normalize_unit(arguments.get("to_unit"))
    ensure_supported(from_unit, factors, category)
    ensure_supported(to_unit, factors, category)

    output_value = value * factors[from_unit] / factors[to_unit]
    return {
        "input_value": value,
        "input_unit": from_unit,
        "output_value": output_value,
        "output_unit": to_unit,
        "category": category,
    }


def convert_temperature(arguments: dict[str, Any]) -> dict[str, Any]:
    value = validate_value(arguments.get("value"))
    from_unit = normalize_unit(arguments.get("from_unit"))
    to_unit = normalize_unit(arguments.get("to_unit"))
    ensure_supported_temperature(from_unit)
    ensure_supported_temperature(to_unit)

    celsius = temperature_to_celsius(value, from_unit)
    output_value = celsius_to_temperature(celsius, to_unit)
    return {
        "input_value": value,
        "input_unit": from_unit,
        "output_value": output_value,
        "output_unit": to_unit,
        "category": "temperature",
    }


def validate_value(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("value must be a finite number.") from error
    if not isfinite(number):
        raise ValueError("value must be a finite number.")
    return number


def normalize_unit(unit: Any) -> str:
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("unit must be a non-empty string.")
    normalized = unit.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    return ALIASES.get(normalized, normalized)


def ensure_supported(unit: str, factors: dict[str, float], category: str) -> None:
    if unit not in factors:
        raise ValueError(
            f"unsupported {category} unit '{unit}'. Supported units: {', '.join(factors)}."
        )


def ensure_supported_temperature(unit: str) -> None:
    if unit not in TEMPERATURE_UNITS:
        raise ValueError(
            f"unsupported temperature unit '{unit}'. Supported units: {', '.join(TEMPERATURE_UNITS)}."
        )


def temperature_to_celsius(value: float, unit: str) -> float:
    if unit == "c":
        return value
    if unit == "f":
        return (value - 32.0) * 5.0 / 9.0
    if unit == "k":
        return value - 273.15
    raise ValueError(f"unsupported temperature unit '{unit}'.")


def celsius_to_temperature(value: float, unit: str) -> float:
    if unit == "c":
        return value
    if unit == "f":
        return (value * 9.0 / 5.0) + 32.0
    if unit == "k":
        return value + 273.15
    raise ValueError(f"unsupported temperature unit '{unit}'.")


def tool_success(result: dict[str, Any]) -> dict[str, Any]:
    text = (
        f"{format_number(result['input_value'])} {result['input_unit']} = "
        f"{format_number(result['output_value'])} {result['output_unit']}"
    )
    return {"content": [{"type": "text", "text": text}], "isError": False}


def tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(float(f"{value:.12g}"))


def json_rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def json_rpc_error(
    request_id: Any,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


INSPECT_CONSOLE_SOURCE = "inspect-console"
# Schema fields the /inspect and /send pages need to turn a config code into a change summary.
_INSPECT_OPTION_KEYS = ("path", "label", "kind", "appliesTo", "strictOnly", "dependsOn")


def _summary_options(meta: dict[str, Any]) -> list[dict[str, Any]]:
    return [{key: option[key] for key in _INSPECT_OPTION_KEYS if key in option} for option in meta["options"]]


def _header_param_map() -> dict[str, list[dict[str, str]]]:
    """Arguments each 2026 tool mirrors into Mcp-Param-* headers."""
    return {
        tool["name"]: [
            {"header": header, "argument": ".".join(path)}
            for path, header in header_param_properties(tool.get("inputSchema"))
        ]
        for tool in modern_tool_definitions(default_config())
        if header_param_properties(tool.get("inputSchema"))
    }


def render_inspect_page() -> str:
    """Viewer of recent /mcp exchanges: address picker, request list and detail.

    The page loads /inspect/log once and then asks only for newer entries
    (``since=<seq>``), adding rows instead of drawing the list again.
    """
    meta = scenario_meta()
    page_meta = {
        "errorCodes": meta["errorCodes"],
        "defaults": meta["defaults"],
        "options": _summary_options(meta),
        "headerParams": _header_param_map(),
        "eraLabels": SERVER_ERA_LABELS,
        "logSize": INSPECT_LOG.maxlen,
        "modernVersion": PROTOCOL_VERSION_2026,
    }
    return _fill_template(
        _INSPECT_PAGE,
        {
            "BASE_CSS": UI_BASE_CSS,
            "NAV_CSS": UI_NAV_CSS,
            "NAV": render_nav("inspect"),
            "LOG_SIZE": str(INSPECT_LOG.maxlen),
            "UI_CORE_JS": UI_CORE_JS,
            "PAGE_JS": _fill_template(_INSPECT_PAGE_JS, {"META_JSON": _script_json(page_meta)}),
        },
    )


_INSPECT_PAGE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unit Expert MCP · 요청 기록</title>
  <style>
    __BASE_CSS__
    __NAV_CSS__
    main { margin-top: 16px; }
    .caution { margin-top: 2px; color: var(--muted); font-size: 12px; }
    pre {
      margin: 0; padding: 10px 12px; border-radius: 6px; background: #101818; color: #d9f2ed;
      white-space: pre-wrap; overflow-wrap: anywhere; max-height: 420px; overflow: auto; line-height: 1.5;
    }
    .field { display: grid; gap: 3px; min-width: 0; align-content: start; }
    .field label, .field-title { font-size: 12px; font-weight: 600; color: var(--muted); }
    .check { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; white-space: nowrap; }

    .address { margin-top: 12px; display: grid; gap: 10px; background: var(--soft); border-color: var(--soft-line); }
    .address h2 { margin: 0; }
    .cfg-input { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
    .cfg-input input { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; padding: 10px 12px; }
    .cfg-input button { padding: 9px 22px; font-size: 14px; }
    .scope { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
    .cfg-summary { display: flex; gap: 6px 12px; flex-wrap: wrap; align-items: center; font-size: 13px; }
    .cfg-summary[hidden], #addrBox[hidden] { display: none; }
    #addrBox h3 { margin: 4px 0 6px; font-size: 13px; color: var(--muted); }
    .addr-list { list-style: none; margin: 0; padding: 0; display: flex; gap: 6px; flex-wrap: wrap; max-height: 132px; overflow: auto; }
    .addr-item {
      display: grid; gap: 0; text-align: left; background: #ffffff; color: var(--text); border-color: var(--soft-line);
      padding: 5px 10px; max-width: 420px; font-weight: 400;
    }
    .addr-item:hover { background: #ffffff; border-color: var(--accent); }
    .addr-item[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); }
    .addr-title { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .addr-meta { font-size: 11px; color: var(--muted); }

    .filters { margin-top: 10px; padding: 8px 16px; }
    .filters > summary { cursor: pointer; font-weight: 600; font-size: 13px; }
    .filters[open] > summary { margin-bottom: 8px; }
    .filter-body { display: grid; gap: 8px; padding-bottom: 6px; }
    .filters input[type="text"], .filters input[type="search"], .filters select, .filters button {
      padding-top: 5px; padding-bottom: 5px;
    }
    .filter-grid { display: grid; grid-template-columns: minmax(220px, 2fr) repeat(4, minmax(130px, 1fr)); gap: 10px; }
    .filter-row { display: flex; align-items: center; gap: 8px 18px; flex-wrap: wrap; }
    .group-badge {
      font-size: 11px; font-weight: 700; color: var(--changed); background: var(--changed-soft);
      border: 1px solid var(--changed-line); border-radius: 999px; padding: 1px 8px; margin-left: 6px;
    }
    .group-badge[hidden] { display: none; }

    .list-head { justify-content: flex-start; gap: 6px 14px; flex-wrap: wrap; margin-bottom: 0; }
    .list-head .count { font-weight: 700; }
    .list-head .spacer { flex: 1 1 auto; }
    .banner {
      margin-top: 10px; padding: 8px 14px; border-radius: 8px; border: 1px solid var(--changed-line);
      background: var(--changed-soft); display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    }
    .banner[hidden] { display: none; }
    .banner > span, .banner > .banner-text { flex: 1 1 320px; min-width: 0; }

    .workspace { margin-top: 10px; display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; align-items: start; }
    .workspace.has-detail { grid-template-columns: minmax(0, 1.3fr) minmax(400px, 1fr); }
    .list-card { padding: 8px 0 0; overflow: hidden; }
    .list-card > .card-head { padding: 0 16px; }
    .table-wrap { margin-top: 8px; overflow: auto; max-height: calc(100vh - 150px); overflow-anchor: none; border-top: 1px solid var(--line); }
    table { border-collapse: collapse; width: 100%; }
    .log-table { font-size: 12px; }
    .log-table th {
      position: sticky; top: 0; z-index: 1; background: #f4f6fa; color: var(--muted); font-weight: 600;
      text-align: left; padding: 6px 8px; white-space: nowrap; border-bottom: 1px solid var(--line);
    }
    .log-table td { padding: 6px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; vertical-align: middle; }
    .log-table td.num, .log-table th.num { text-align: right; }
    .log-table tbody tr { cursor: pointer; }
    .log-table tbody tr[hidden] { display: none; }
    .log-table tbody tr:hover { background: #f4f6fa; }
    .log-table tbody tr[aria-current="true"] { background: var(--accent-soft); box-shadow: inset 3px 0 0 var(--accent); }
    .log-table tbody tr:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
    .log-table .method { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-weight: 600; }
    .log-table .tool { color: var(--muted); font-weight: 400; }
    .log-table .time, .log-table .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .cfg-button {
      border: 1px solid var(--soft-line); background: #ffffff; color: var(--accent-strong); padding: 1px 6px;
      font-size: 12px; font-weight: 500;
    }
    .cfg-button:hover { background: #ffffff; border-color: var(--accent); }
    .badge {
      display: inline-block; border-radius: 6px; padding: 0 7px; font-size: 11px; font-weight: 700;
      white-space: nowrap; border: 1px solid transparent; max-width: 100%;
    }
    .kind-ok { background: #e7f6ee; color: var(--success); border-color: #b5e0c7; }
    .kind-injected { background: var(--changed-soft); color: var(--changed); border-color: var(--changed-line); }
    .kind-rejected { background: var(--danger-soft); color: var(--danger); border-color: #f0b4ad; }
    .kind-raw { background: #efeafc; color: #5b3aa8; border-color: #cdbff0; }
    .kind-closed { background: #e8eaee; color: #343b46; border-color: #bcc3ce; }
    .tone-ok { color: var(--success); font-weight: 700; }
    .tone-err { color: var(--danger); font-weight: 700; }
    .inject-badge { background: var(--changed-soft); color: var(--changed); border-color: var(--changed-line); font-weight: 600; }
    .empty { padding: 28px 16px; text-align: center; color: var(--muted); display: grid; gap: 10px; justify-items: center; font-size: 14px; }
    .empty[hidden], .empty [hidden] { display: none; }
    .empty-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; }

    .detail { position: sticky; top: 12px; max-height: calc(100vh - 24px); overflow: auto; scrollbar-gutter: stable; }
    .detail[hidden] { display: none; }
    .d-section { padding: 12px 0; border-top: 1px solid var(--line); display: grid; gap: 6px; min-width: 0; }
    .d-section:first-child { border-top: 0; padding-top: 0; }
    .d-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
    .d-head h3 { margin: 0; }
    .d-actions { display: flex; gap: 6px 12px; flex-wrap: wrap; align-items: center; }
    .d-summary { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; font-weight: 600; }
    .d-facts { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
    .d-section > .hint:empty { display: none; }
    .reason {
      border: 1px solid #f0b4ad; background: var(--danger-soft); border-radius: 8px; padding: 10px 12px;
      display: grid; gap: 4px; overflow-wrap: anywhere;
    }
    .reason.neutral { border-color: var(--changed-line); background: var(--changed-soft); }
    .reason strong { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .kv-table { font-size: 12px; table-layout: fixed; }
    .kv-table th, .kv-table td {
      text-align: left; vertical-align: top; padding: 4px 8px; border-bottom: 1px solid var(--line);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; font-weight: 400;
    }
    .kv-table thead th { font-family: inherit; color: var(--muted); font-weight: 600; background: #f4f6fa; }
    .kv-table tbody th { width: 34%; font-weight: 600; }
    .compare { table-layout: auto; }
    .compare tbody th, .compare td { width: auto; }
    .decoded { display: block; color: var(--accent-strong); }
    .token { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--muted); }
    .event-title { font-size: 12px; font-weight: 600; color: var(--muted); }
    @media (min-width: 1101px) {
      /* Beside the open detail the list keeps the columns that tell requests
         apart. Their widths are fixed and long values wrap, so the result
         and the error code are never cut off or pushed out of view. */
      .workspace.has-detail .c-http, .workspace.has-detail .c-inj,
      .workspace.has-detail .c-ms, .workspace.has-detail .c-cfg { display: none; }
      .workspace.has-detail .log-table { table-layout: fixed; }
      .workspace.has-detail .log-table th, .workspace.has-detail .log-table td {
        white-space: normal; overflow-wrap: anywhere; padding-left: 6px; padding-right: 6px;
      }
      .workspace.has-detail .c-time { width: 16%; }
      .workspace.has-detail .c-method { width: 24%; }
      .workspace.has-detail .c-mode { width: 11%; }
      .workspace.has-detail .c-era { width: 11%; }
      .workspace.has-detail .c-status { width: 9%; }
      .workspace.has-detail .c-code { width: 16%; }
      .workspace.has-detail .c-kind { width: 13%; }
      .workspace.has-detail .log-table .tool, .workspace.has-detail .log-table .code-name { display: block; }
      .workspace.has-detail .log-table .code-name { font-size: 11px; }
    }
    @media (max-width: 1100px) {
      .workspace.has-detail { grid-template-columns: minmax(0, 1fr); }
      .detail { position: static; max-height: none; overflow: visible; }
      .table-wrap { max-height: 60vh; }
      .filter-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .filter-grid .wide { grid-column: 1 / -1; }
    }
    @media (max-width: 620px) {
      .cfg-input { grid-template-columns: minmax(0, 1fr); }
      .addr-item { max-width: 100%; }
    }
  </style>
</head>
<body>
  __NAV__
  <main>
    <h1>요청 기록</h1>
    <p class="lead">MCP 주소로 온 요청과 서버가 보낸 응답입니다.</p>
    <p class="caution" role="note">최근 __LOG_SIZE__건을 모든 사용자가 함께 봅니다. 요청 헤더가 그대로 남으니 실제 서비스 토큰은 보내지 마세요.</p>

    <section class="card address" aria-labelledby="addrTitle">
      <h2 id="addrTitle"><label for="fCfg">어느 주소로 온 요청을 볼까요?</label></h2>
      <div class="cfg-input">
        <input type="text" id="fCfg" placeholder="MCP 주소를 붙여 넣으세요" autocomplete="off" spellcheck="false">
        <button type="button" id="applyCfg">보기</button>
      </div>
      <p id="cfgError" class="error-text" role="alert"></p>
      <div class="scope" role="group" aria-label="볼 범위">
        <button type="button" class="secondary small" id="scopeAll" aria-pressed="false">모든 주소</button>
        <button type="button" class="secondary small" id="scopePlain" aria-pressed="false">기본 주소만 (설정 없는 /mcp)</button>
      </div>
      <div id="cfgSummary" class="cfg-summary" hidden>
        <strong id="cfgSummaryText"></strong>
        <ul id="cfgChips" class="chips"></ul>
        <a id="cfgScenarioLink" href="/scenario">서버 설정에서 열기</a>
        <a id="cfgSendLink" href="/send">이 주소로 요청 보내기</a>
      </div>
      <div id="addrBox" hidden>
        <h3 id="addrListTitle">최근 요청이 온 주소</h3>
        <ul id="addrList" class="addr-list" aria-labelledby="addrListTitle"></ul>
      </div>
    </section>

    <details class="card filters" id="filterBox">
      <summary>자세히 거르기<span id="filterCount" class="group-badge" hidden></span></summary>
      <div class="filter-body">
        <div class="filter-grid">
          <div class="field wide">
            <label for="fText">검색</label>
            <input type="search" id="fText" placeholder="메서드, 툴 이름, 헤더, 바디, 에러 이름" autocomplete="off" spellcheck="false">
          </div>
          <div class="field">
            <label for="fKind">결과</label>
            <select id="fKind">
              <option value="">전체</option>
              <option value="ok">정상</option>
              <option value="injected">넣은 에러</option>
              <option value="rejected">서버가 거절</option>
              <option value="raw">직접 지정한 응답</option>
              <option value="closed">연결 종료</option>
            </select>
          </div>
          <div class="field">
            <label for="fStatus">HTTP 상태</label>
            <select id="fStatus">
              <option value="">전체</option>
              <option value="2xx">2xx</option>
              <option value="4xx">4xx</option>
              <option value="5xx">5xx</option>
              <option value="0">연결 종료</option>
            </select>
          </div>
          <div class="field">
            <label for="fEra">요청 방식</label>
            <select id="fEra">
              <option value="">전체</option>
              <option value="2025">2025 방식</option>
              <option value="2026">2026 방식</option>
            </select>
          </div>
          <div class="field">
            <label for="fMethod">메서드</label>
            <select id="fMethod">
              <option value="">전체</option>
            </select>
          </div>
        </div>
        <div class="filter-row">
          <label class="check"><input type="checkbox" id="fHideScreen"> 요청 보내기 화면에서 보낸 요청 숨기기</label>
          <label class="check"><input type="checkbox" id="fHideOptions"> OPTIONS 숨기기</label>
          <label class="check"><input type="checkbox" id="fErrorsOnly"> 에러만</label>
          <button type="button" class="secondary" id="resetFilters">처음으로</button>
        </div>
      </div>
    </details>

    <div id="hiddenBanner" class="banner" role="status" hidden>
      <span id="hiddenBannerText"></span>
      <button type="button" class="secondary" id="unhideBtn">다시 보기</button>
    </div>
    <div id="newBanner" class="banner" role="status" hidden>
      <div class="banner-text"><span id="newBannerText"></span> <span id="newBannerWhy"></span></div>
      <button type="button" class="secondary" id="newBannerReset">가린 필터 풀기</button>
    </div>
    <p id="liveStatus" class="error-text" role="status" aria-live="polite"></p>
    <p id="announce" class="sr-only" aria-live="polite"></p>

    <div class="workspace" id="workspace">
      <section class="card list-card" aria-labelledby="listTitle">
        <div class="card-head list-head">
          <h2 id="listTitle" class="sr-only">요청 목록</h2>
          <span class="count" id="resultCount" aria-live="polite">불러오는 중</span>
          <span class="hint" id="lastChecked"></span>
          <span class="spacer"></span>
          <label class="check"><input type="checkbox" id="liveToggle"> 2초마다 새로 읽기</label>
          <button type="button" class="secondary small" id="refreshBtn">지금 읽기</button>
          <button type="button" class="secondary small" id="hideBtn">지금까지 기록 숨기기</button>
        </div>
        <p class="sr-only" id="listKeysHint">Enter로 상세를 열고 위아래 화살표로 행을 옮깁니다.</p>
        <div class="table-wrap" id="tableWrap">
          <table class="log-table" aria-labelledby="listTitle" aria-describedby="listKeysHint">
            <thead>
              <tr>
                <th scope="col" class="c-time">시각</th>
                <th scope="col" class="c-http">HTTP</th>
                <th scope="col" class="c-method">메서드</th>
                <th scope="col" class="c-mode" title="클라이언트가 요청을 보낸 방식">요청 방식</th>
                <th scope="col" class="c-era" title="그 주소의 서버가 지원하는 스펙">서버</th>
                <th scope="col" class="c-status">상태</th>
                <th scope="col" class="c-code">에러 코드</th>
                <th scope="col" class="c-kind">결과</th>
                <th scope="col" class="c-inj">넣은 에러</th>
                <th scope="col" class="num c-ms">ms</th>
                <th scope="col" class="c-cfg">주소</th>
              </tr>
            </thead>
            <tbody id="rows"></tbody>
          </table>
        </div>
        <div class="empty" id="empty" hidden>
          <p id="emptyText"></p>
          <div class="empty-actions">
            <button type="button" id="emptyShow" hidden>보기</button>
            <button type="button" class="secondary" id="emptyReset">모든 주소, 필터 처음으로</button>
            <a class="button secondary" href="/scenario">서버 설정으로</a>
          </div>
        </div>
      </section>

      <aside class="card detail" id="detail" aria-labelledby="detailTitle" hidden>
        <div class="card-head">
          <h2 id="detailTitle">요청 상세</h2>
          <button type="button" class="secondary" id="closeDetail">닫기</button>
        </div>
        <div id="detailBody"></div>
        <p id="detailStatus" class="status-line" aria-live="polite"></p>
      </aside>
    </div>
  </main>
  <script>
__UI_CORE_JS__
  </script>
  <script>
__PAGE_JS__
  </script>
</body>
</html>"""


_INSPECT_PAGE_JS = r"""(function () {
  "use strict";
  const UI = UnitExpertUI;
  const META = __META_JSON__;
  const ERROR_CODES = META.errorCodes;
  const DEFAULTS = META.defaults;
  const SCHEMA = META.options;
  const POLL_MS = 2000;
  const PROBE_EVERY = 15;
  // The page holds as many rows as the server keeps entries.
  const MAX_ROWS = META.logSize;
  const RECHECK_LIMIT = 5;
  const KNOWN_METHODS = [
    "initialize", "notifications/initialized", "server/discover", "tools/list", "tools/call",
    "ping", "subscriptions/listen"
  ];
  // Request mode names, and what each means (shown as a tooltip only).
  const ERA_SHORT = { "2025": "2025 방식", "2026": "2026 방식" };
  const ERA_MEANING = {
    "2025": "initialize로 시작하는 요청",
    "2026": "요청마다 버전(" + META.modernVersion + ")을 싣는 요청"
  };
  const MODE_UNKNOWN = "알 수 없음 (JSON-RPC 요청이 아님)";
  const ERA_UNKNOWN = "정해지기 전에 끝난 요청";
  // What the server supports (serverEra), in plain words.
  const SERVER_ERA_LABELS = META.eraLabels;
  // What each filter is called on screen, and the value that lets everything through.
  const FILTER_NAMES = {
    text: "검색어", kind: "결과", status: "HTTP 상태", era: "요청 방식", method: "메서드",
    errorsOnly: "에러만", hideScreen: "요청 보내기 화면에서 보낸 요청 숨기기", hideOptions: "OPTIONS 숨기기"
  };
  const FILTER_OFF = {
    text: "", kind: "", status: "", era: "", method: "",
    errorsOnly: false, hideScreen: false, hideOptions: false
  };
  const CONFIG_SOURCE_LABELS = {
    url: "주소에 담긴 설정",
    header: UI.SCENARIO_HEADER + " 헤더",
    global: "서버 기본값 (주소에도 헤더에도 설정이 없는 요청)"
  };
  // The same three sources as the list writes them.
  const CONFIG_SOURCE_SHORT = { url: "주소", header: "헤더", global: "기본" };

  const byId = (id) => document.getElementById(id);
  const rowsEl = byId("rows");
  const wrapEl = byId("tableWrap");
  const liveToggle = byId("liveToggle");

  // seq -> {x, row, durationCell, info}. order holds the seqs, newest first.
  const store = new Map();
  let order = [];
  let filters = UI.filtersFromQuery(location.search);
  let lastSeq = 0;
  let recheck = [];
  let hideBefore = 0;
  let selectedSeq = null;
  let selectedExchange = null;
  let focusSeq = null;
  let revealSecrets = false;
  let hiddenNew = [];
  let blockingKeys = [];
  let firstLoad = true;
  let polling = false;
  let generation = 0;
  let pollCount = 0;
  let detailRefs = null;
  let notice = "";
  let noticeUntil = 0;
  let addressRows = [];
  const methodOptions = new Set();
  const summaryCache = new Map();

  function setText(id, text) {
    const node = byId(id);
    if (node.textContent !== text) node.textContent = text;
  }

  function el(tag, attrs) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach((key) => {
      const value = attrs[key];
      if (value === false || value === null || value === undefined) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value === true ? "" : String(value));
    });
    for (let index = 2; index < arguments.length; index += 1) {
      const child = arguments[index];
      if (child !== null && child !== undefined && child !== false) node.append(child);
    }
    return node;
  }

  function pretty(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch (error) { return String(value); }
  }

  function readJson(text) {
    try { return JSON.parse(text); } catch (error) { return null; }
  }

  function summaryOf(token) {
    if (!summaryCache.has(token)) summaryCache.set(token, UI.tokenSummary(token, DEFAULTS, SCHEMA));
    return summaryCache.get(token);
  }

  function statusText(exchange) {
    // status 0: the server closed the connection without a response.
    if (!exchange.status) return "연결 종료";
    return String(exchange.status);
  }

  function statusTone(x) {
    return x.status >= 200 && x.status < 400 ? "tone-ok" : "tone-err";
  }

  function injectedOptions(x) {
    return x.injected || [];
  }

  function durationText(x) {
    return typeof x.durationMs === "number" ? String(x.durationMs) : "-";
  }

  function codeText(info) {
    return info.code === null ? "" : info.code + (info.codeName ? " " + info.codeName : "");
  }

  function codeDescription(code) {
    const known = ERROR_CODES.filter((entry) => entry.code === code)[0];
    return known ? known.description : "";
  }

  // ---- filters -----------------------------------------------------------

  function exchangeFilters() {
    return {
      // null: every address. "": only the address without a config.
      cfg: filters.cfg || (filters.plain ? "" : null),
      method: filters.method,
      era: filters.era,
      status: filters.status,
      kind: filters.kind,
      errorsOnly: filters.errorsOnly,
      hideScreen: filters.hideScreen,
      hideOptions: filters.hideOptions,
      text: filters.text,
      errorCodes: ERROR_CODES
    };
  }

  function matches(x, active) {
    return UI.filterExchanges([x], active).length === 1;
  }

  function addMethodOption(method) {
    if (!method || methodOptions.has(method)) return;
    methodOptions.add(method);
    byId("fMethod").append(el("option", { value: method, text: method }));
  }

  function writeFilterControls() {
    addMethodOption(filters.method);
    byId("fText").value = filters.text;
    byId("fKind").value = filters.kind;
    byId("fStatus").value = filters.status;
    byId("fEra").value = filters.era;
    byId("fMethod").value = filters.method;
    // The field shows the address the way the config screen copies it.
    byId("fCfg").value = filters.cfg ? UI.buildMcpUrl(location.origin, filters.cfg) : (filters.plain ? UI.buildMcpUrl(location.origin, "") : "");
    writeFilterCount();
    byId("fHideScreen").checked = filters.hideScreen;
    byId("fHideOptions").checked = filters.hideOptions;
    byId("fErrorsOnly").checked = filters.errorsOnly;
  }

  function chipNode(chip) {
    return el("li", {
      class: "chip" + (chip.effective ? "" : " is-ineffective"),
      text: chip.label + " = " + chip.value + (chip.effective ? "" : " (적용 안 됨)"),
      title: chip.path + (chip.reason ? " · " + chip.reason : "")
    });
  }

  /* Which address the list shows: the two scope buttons, and for a chosen
     address what its config changes with links to the other two screens. */
  function writeCfgSummary() {
    const box = byId("cfgSummary");
    byId("scopeAll").setAttribute("aria-pressed", !filters.cfg && !filters.plain ? "true" : "false");
    byId("scopePlain").setAttribute("aria-pressed", !filters.cfg && filters.plain ? "true" : "false");
    renderAddresses();
    if (!filters.cfg) {
      box.hidden = true;
      return;
    }
    const summary = summaryOf(filters.cfg);
    let text = "이 주소에서 바꾼 설정";
    if (!summary.ok) text = "주소의 설정을 읽지 못했습니다. 주소가 잘리지 않았는지 확인하세요.";
    else if (!summary.chips.length) text = "기본 설정과 같은 주소입니다.";
    setText("cfgSummaryText", text);
    byId("cfgChips").replaceChildren(...summary.chips.map(chipNode));
    byId("cfgScenarioLink").href = "/scenario#cfg=" + encodeURIComponent(filters.cfg);
    byId("cfgSendLink").href = "/send?cfg=" + encodeURIComponent(filters.cfg);
    box.hidden = false;
  }

  /* The addresses that got requests, newest first. Pressing one shows only
     its requests. */
  function renderAddresses() {
    const list = byId("addrList");
    byId("addrBox").hidden = addressRows.length === 0;
    list.replaceChildren(...addressRows.map((row) => {
      const chosen = row.plain ? !filters.cfg && filters.plain : filters.cfg === row.cfg;
      const button = el(
        "button",
        { type: "button", class: "addr-item", "aria-pressed": chosen ? "true" : "false", title: row.plain ? "/mcp" : "/mcp?cfg=" + row.cfg },
        el("span", { class: "addr-title", text: row.title }),
        el("span", {
          class: "addr-meta",
          text: "요청 " + row.count + "건" + (row.screen ? " (요청 보내기 화면 " + row.screen + "건)" : "") +
            " · 마지막 " + (UI.formatLocalStamp(row.lastTimestamp).slice(0, -4) || "-")
        })
      );
      button.addEventListener("click", () => { setScope(row.cfg, row.plain); });
      return el("li", null, button);
    }));
  }

  /* The detailed filters are folded. The summary says how many are on. */
  function writeFilterCount() {
    const defaults = UI.defaultFilters();
    const count = ["text", "kind", "status", "era", "method", "errorsOnly", "hideScreen", "hideOptions"]
      .filter((key) => filters[key] !== defaults[key]).length;
    const badge = byId("filterCount");
    badge.hidden = count === 0;
    badge.textContent = "바꿈 " + count;
    return count;
  }

  function syncUrl() {
    history.replaceState(null, "", location.pathname + UI.filtersToQuery(filters));
  }

  function isVisible(record, active) {
    return record.x.seq > hideBefore && matches(record.x, active);
  }

  function applyFilters() {
    const active = exchangeFilters();
    store.forEach((record) => { record.row.hidden = !isVisible(record, active); });
    hiddenNew = [];
    syncTabStop();
    updateCounts();
  }

  /* Which filters keep the given records out of the list: {keys, text}.
     keys are filter names, text reads "검색어 3건, OPTIONS 숨기기 1건". */
  function blockersOf(records, active) {
    const counts = {};
    records.forEach((record) => {
      UI.filterBlockers(record.x, active).forEach((key) => {
        if (FILTER_NAMES[key]) counts[key] = (counts[key] || 0) + 1;
      });
    });
    const keys = Object.keys(FILTER_NAMES).filter((key) => counts[key]);
    return {
      keys: keys,
      counts: counts,
      text: keys.map((key) => FILTER_NAMES[key] + " " + counts[key] + "건").join(", ")
    };
  }

  /* Turns off exactly the named filters. */
  function releaseFilters(keys) {
    keys.forEach((key) => { filters[key] = FILTER_OFF[key]; });
    writeFilterControls();
    filtersChanged();
  }

  function filtersChanged() {
    syncUrl();
    writeFilterCount();
    applyFilters();
  }

  /* Chooses whose requests are listed: one address (token), the address
     without a config (plain), or every address. */
  function setScope(token, plain) {
    const nextPlain = !token && Boolean(plain);
    if (token === filters.cfg && nextPlain === filters.plain) return;
    filters.cfg = token;
    filters.plain = nextPlain;
    setText("cfgError", "");
    writeFilterControls();
    writeCfgSummary();
    syncUrl();
    resetStore();
    poll();
  }

  function setCfg(token) {
    setScope(token, false);
  }

  function applyCfgInput() {
    const raw = byId("fCfg").value.trim();
    if (!raw) {
      setScope("", false);
      return;
    }
    const token = UI.tokenFromText(raw);
    if (!token) {
      // An address without cfg= is the address without a config.
      if (/\/mcp\/?(\?|#|$)/.test(raw)) {
        setScope("", true);
        return;
      }
      setText("cfgError", "MCP 주소로 읽지 못했습니다. 서버 설정 화면의 '주소 복사'로 복사한 주소를 붙여 넣으세요.");
      return;
    }
    setScope(token, false);
  }

  function resetFilters() {
    const hadCfg = Boolean(filters.cfg) || filters.plain;
    filters = UI.defaultFilters();
    setText("cfgError", "");
    writeFilterControls();
    writeCfgSummary();
    syncUrl();
    if (hadCfg) {
      resetStore();
      poll();
    } else {
      applyFilters();
    }
  }

  // ---- list --------------------------------------------------------------

  function badge(kind, label, title) {
    return el("span", { class: "badge kind-" + kind, text: label, title: title });
  }

  function configCell(x) {
    if (x.configSource === "url" && x.cfg) {
      const summary = summaryOf(x.cfg);
      const button = el("button", {
        type: "button",
        class: "cfg-button",
        // What the address changes, cut short. An unreadable code shows its start.
        text: summary.ok ? (summary.text.length > 26 ? summary.text.slice(0, 25) + "…" : summary.text) : x.cfg.slice(0, 8) + "…",
        title: "바꾼 설정: " + summary.text + "\n누르면 이 주소로 온 요청만 봅니다.",
        "aria-label": "이 주소로 온 요청만 보기. 바꾼 설정: " + summary.text,
        // Not a tab stop of its own: the detail has the same action as a button.
        tabindex: "-1"
      });
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        setCfg(x.cfg);
      });
      button.addEventListener("keydown", (event) => { event.stopPropagation(); });
      return el("td", { class: "c-cfg" }, button);
    }
    return el("td", {
      class: "c-cfg",
      text: CONFIG_SOURCE_SHORT[x.configSource] || x.configSource || "-",
      title: CONFIG_SOURCE_LABELS[x.configSource] || "설정을 읽기 전에 끝난 요청"
    });
  }

  /* "-32020 HeaderMismatch". The name may wrap between its words. */
  function codeCell(info) {
    const cell = el("td", { class: "mono", title: info.code === null ? "" : codeDescription(info.code) });
    if (info.code === null) {
      cell.textContent = "-";
      return cell;
    }
    cell.append(el("span", { text: String(info.code) }));
    if (info.codeName) {
      const name = el("span", { class: "code-name" });
      info.codeName.split(/(?=[A-Z])/).forEach((word, index) => {
        if (index) name.append(el("wbr"));
        name.append(word);
      });
      cell.append(" ", name);
    }
    return cell;
  }

  function buildRow(x, info) {
    const tool = UI.toolName(x);
    const injected = injectedOptions(x);
    const mode = UI.requestMode(x);
    const methodCell = el("td", { class: "method", text: x.method || "-" });
    if (tool) methodCell.append(el("span", { class: "tool", text: " " + tool }));
    const injectedCell = el("td", { class: "c-inj" });
    if (injected.length) {
      injectedCell.append(el("span", {
        class: "badge inject-badge",
        text: injected[0] + (injected.length > 1 ? " 외 " + (injected.length - 1) : ""),
        title: (info.delays.length === injected.length ? "늦게 답함: " : "넣은 에러: ") + injected.join(", ")
      }));
    }
    const durationCell = el("td", { class: "num mono c-ms", text: durationText(x) });
    const row = el(
      "tr",
      { tabindex: "-1", "data-seq": x.seq },
      el("td", { class: "time", text: UI.formatLocalStamp(x.timestamp) || x.time || "", title: x.timestamp || "" }),
      el("td", { class: "mono c-http", text: x.httpMethod || "" }),
      methodCell,
      // A request whose mode or server generation cannot be told shows "-".
      el("td", { text: ERA_SHORT[mode] || "-", title: ERA_MEANING[mode] || MODE_UNKNOWN }),
      el("td", { text: SERVER_ERA_LABELS[x.serverEra] || x.serverEra || "-", title: x.serverEra ? "serverEra: " + x.serverEra : ERA_UNKNOWN }),
      el("td", { class: statusTone(x), text: statusText(x) }),
      codeCell(info),
      el("td", null, badge(info.kind, info.label, info.detail)),
      injectedCell,
      durationCell,
      configCell(x)
    );
    row.addEventListener("click", () => {
      // A drag that selects text is not a click on the row.
      if (window.getSelection && String(window.getSelection())) return;
      focusSeq = x.seq;
      syncTabStop();
      openDetail(x.seq, true);
    });
    row.addEventListener("keydown", (event) => { onRowKey(event, x.seq); });
    row.addEventListener("focus", () => {
      focusSeq = x.seq;
      syncTabStop();
    });
    return { row: row, durationCell: durationCell };
  }

  function visibleSeqs() {
    return order.filter((seq) => !store.get(seq).row.hidden);
  }

  /* Exactly one visible row is in the tab order (the last focused one). */
  function syncTabStop() {
    const visible = visibleSeqs();
    if (visible.indexOf(focusSeq) < 0) focusSeq = visible.indexOf(selectedSeq) >= 0 ? selectedSeq : (visible.length ? visible[0] : null);
    store.forEach((record, seq) => {
      record.row.setAttribute("tabindex", seq === focusSeq ? "0" : "-1");
    });
  }

  function focusRow(seq) {
    const record = store.get(seq);
    if (!record) return;
    focusSeq = seq;
    syncTabStop();
    record.row.focus();
  }

  function onRowKey(event, seq) {
    const visible = visibleSeqs();
    const index = visible.indexOf(seq);
    let target = null;
    if (event.key === "ArrowDown") target = visible[Math.min(index + 1, visible.length - 1)];
    else if (event.key === "ArrowUp") target = visible[Math.max(index - 1, 0)];
    else if (event.key === "Home") target = visible[0];
    else if (event.key === "End") target = visible[visible.length - 1];
    else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openDetail(seq);
      return;
    } else if (event.key === "Escape" && selectedSeq !== null) {
      closeDetail();
      return;
    } else {
      return;
    }
    event.preventDefault();
    if (target !== undefined && target !== null) {
      focusRow(target);
      // With the detail open, moving through the list shows each request.
      if (selectedSeq !== null) openDetail(target);
    }
  }

  function addRow(x, active) {
    const info = UI.classifyExchange(x, ERROR_CODES);
    const built = buildRow(x, info);
    const record = { x: x, row: built.row, durationCell: built.durationCell, info: info };
    store.set(x.seq, record);
    order.unshift(x.seq);
    addMethodOption(x.method);
    const visible = isVisible(record, active);
    record.row.hidden = !visible;
    if (x.seq === selectedSeq) record.row.setAttribute("aria-current", "true");
    rowsEl.prepend(record.row);
    if (!visible && !firstLoad) hiddenNew.push(x.seq);
    return visible;
  }

  function trimRows() {
    while (order.length > MAX_ROWS) {
      const seq = order.pop();
      const record = store.get(seq);
      store.delete(seq);
      if (record) record.row.remove();
    }
  }

  function updateDuration(record, x) {
    if (record.x.durationMs === x.durationMs) return;
    record.x.durationMs = x.durationMs;
    record.durationCell.textContent = durationText(record.x);
    if (detailRefs && detailRefs.seq === x.seq) detailRefs.duration.textContent = durationText(record.x) + " ms";
  }

  /* Adds the entries the page does not have yet (list: newest first) on top
     of the table. Rows already there are left alone, apart from durationMs.
     Returns false when the log turns out to belong to a restarted server. */
  function merge(list) {
    const active = exchangeFilters();
    const fresh = [];
    const before = wrapEl.scrollHeight;
    for (let index = list.length - 1; index >= 0; index -= 1) {
      const x = list[index];
      const known = store.get(x.seq);
      if (known) {
        if (known.x.timestamp !== x.timestamp) return false;
        updateDuration(known, x);
        continue;
      }
      if (x.seq <= lastSeq) continue;
      addRow(x, active);
      fresh.push(x.seq);
      lastSeq = x.seq;
    }
    recheck = fresh.slice(-RECHECK_LIMIT);
    trimRows();
    if (fresh.length && wrapEl.scrollTop > 0) {
      // Keep the rows the reader is looking at where they are.
      wrapEl.scrollTop += wrapEl.scrollHeight - before;
    }
    firstLoad = false;
    if (fresh.length) syncTabStop();
    updateCounts();
    return true;
  }

  function resetStore() {
    generation += 1;
    store.clear();
    order = [];
    rowsEl.replaceChildren();
    lastSeq = 0;
    recheck = [];
    hiddenNew = [];
    firstLoad = true;
    focusSeq = null;
    updateCounts();
  }

  function updateCounts() {
    const total = store.size;
    let visible = 0;
    let hiddenByScreen = 0;
    store.forEach((record) => {
      if (!record.row.hidden) visible += 1;
      if (record.x.seq <= hideBefore) hiddenByScreen += 1;
    });
    setText(
      "resultCount",
      firstLoad
        ? "불러오는 중"
        : (filters.cfg ? "이 주소로 온 요청 " : (filters.plain ? "기본 주소로 온 요청 " : "")) +
          (visible === total ? total + "건" : total + "건 중 " + visible + "건")
    );

    const hiddenBanner = byId("hiddenBanner");
    hiddenBanner.hidden = hideBefore === 0;
    if (hideBefore) {
      setText(
        "hiddenBannerText",
        "기록 " + hiddenByScreen + "건을 이 화면에서만 숨겼습니다. 새 요청은 계속 나타납니다."
      );
    }

    const active = exchangeFilters();
    const newBanner = byId("newBanner");
    const newRecords = hiddenNew.map((seq) => store.get(seq)).filter(Boolean);
    newBanner.hidden = newRecords.length === 0;
    if (newRecords.length) {
      const why = blockersOf(newRecords, active);
      newBanner.blockers = why.keys;
      setText("newBannerText", "새 요청 " + newRecords.length + "건이 필터에 가려져 있습니다.");
      setText("newBannerWhy", why.text ? "가린 필터: " + why.text + "." : "");
    }

    const empty = byId("empty");
    empty.hidden = firstLoad || visible > 0;
    if (!empty.hidden) {
      // Records the filters hide (not the ones put away with the hide button).
      const filtered = [];
      store.forEach((record) => { if (record.row.hidden && record.x.seq > hideBefore) filtered.push(record); });
      const why = blockersOf(filtered, active);
      const onlyScreen = why.keys.length === 1 && why.keys[0] === "hideScreen";
      blockingKeys = [];
      let text;
      if (total === 0 && filters.cfg) {
        text = "이 주소로 온 요청이 아직 없습니다. 연결한 주소가 이 주소와 글자 하나까지 같은지 확인하세요.";
      } else if (total === 0 && filters.plain) {
        text = "기본 주소(설정 없는 /mcp)로 온 요청이 아직 없습니다.";
      } else if (total === 0) {
        text = "아직 온 요청이 없습니다. 서버 설정에서 주소를 만들어 연결하면 여기에 나타납니다.";
      } else if (hiddenByScreen === total) {
        text = "지금까지의 기록을 숨겼습니다. 새 요청이 오면 여기에 나타납니다.";
      } else if (onlyScreen) {
        blockingKeys = why.keys;
        text = "요청 보내기 화면에서 보낸 요청 " + filtered.length + "건만 있고, 지금은 숨겨져 있습니다.";
      } else {
        blockingKeys = why.keys;
        text = "필터에 맞는 요청이 없습니다. " + total + "건이 가려져 있습니다" + (why.text ? ": " + why.text : "") + ".";
      }
      setText("emptyText", text);
      const show = byId("emptyShow");
      show.hidden = blockingKeys.length === 0;
      setText("emptyShow", onlyScreen ? "보기" : "가린 필터 풀기");
      byId("emptyReset").hidden = total === 0 && !filters.cfg;
    }
  }

  // ---- loading -----------------------------------------------------------

  async function fetchLog(query) {
    const response = await fetch("/inspect/log" + query, { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    // groups=1 also returns the addresses that got requests.
    if (Array.isArray(data.groups)) {
      addressRows = UI.addressGroups(data.groups, DEFAULTS, SCHEMA);
      renderAddresses();
    }
    return data.exchanges || [];
  }

  /* True when the newest entry on the server cannot follow what the page
     holds: the server was restarted and counts seq from 1 again. */
  async function serverRestarted() {
    if (!lastSeq) return false;
    const newest = (await fetchLog("?limit=1"))[0];
    if (!newest) return true;
    if (newest.seq < lastSeq) return true;
    const known = store.get(newest.seq);
    return Boolean(known && known.x.timestamp !== newest.timestamp);
  }

  async function poll(checkRestart) {
    if (polling) return;
    polling = true;
    const started = generation;
    try {
      pollCount += 1;
      let restarted = (checkRestart || pollCount % PROBE_EVERY === 0) && (await serverRestarted());
      if (!restarted && started === generation) {
        // Entries of the previous poll are asked for once more: their
        // durationMs may have been written before the response was sent.
        const since = recheck.length ? Math.min.apply(null, recheck) - 1 : lastSeq;
        const cfg = filters.cfg;
        // cfg=<token>: that address. An empty cfg=: the address without a config.
        const scope = cfg ? "&cfg=" + encodeURIComponent(cfg) : (filters.plain ? "&cfg=" : "");
        const list = await fetchLog("?since=" + since + scope + "&groups=1");
        if (started === generation) restarted = !merge(list);
      }
      if (restarted && started === generation) {
        if (selectedSeq !== null) closeDetail();
        resetStore();
        notice = "서버가 다시 시작되어 기록을 새로 읽었습니다.";
        noticeUntil = pollCount + 5;
      } else if (started === generation && pollCount > noticeUntil) {
        notice = "";
      }
      setText("liveStatus", notice);
      setText("lastChecked", "마지막 확인 " + UI.formatLocalTime(new Date().toISOString()).slice(0, 8));
    } catch (error) {
      setText("liveStatus", "기록을 가져오지 못했습니다: " + (error && error.message ? error.message : error));
    } finally {
      polling = false;
      if (started !== generation) poll();
    }
  }

  function tick() {
    if (!liveToggle.checked || document.hidden) return;
    poll();
  }

  // ---- detail ------------------------------------------------------------

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.setAttribute("aria-hidden", "true");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    const done = document.execCommand("copy");
    area.remove();
    if (!done) throw new Error("copy failed");
  }

  function copyButton(label, what, getText) {
    const button = el("button", { type: "button", class: "secondary small", text: label });
    button.addEventListener("click", async () => {
      try {
        await copyText(getText());
        setText("detailStatus", "복사했습니다: " + what);
      } catch (error) {
        setText("detailStatus", "복사하지 못했습니다. 글자를 직접 선택해 복사하세요.");
      }
    });
    return button;
  }

  function section(title) {
    const node = el("section", { class: "d-section" });
    if (title) node.append(el("h3", { text: title }));
    for (let index = 1; index < arguments.length; index += 1) {
      const child = arguments[index];
      if (child !== null && child !== undefined && child !== false) node.append(child);
    }
    return node;
  }

  function headerTable(headers, emptyText) {
    const names = Object.keys(headers || {});
    if (!names.length) return el("p", { class: "hint", text: emptyText });
    const body = el("tbody");
    names.forEach((name) => {
      body.append(el("tr", null, el("th", { scope: "row", text: name }), el("td", { text: String(headers[name]) })));
    });
    return el("table", { class: "kv-table" }, body);
  }

  function chipList(summary) {
    const list = el("ul", { class: "chips" });
    summary.chips.forEach((chip) => { list.append(chipNode(chip)); });
    return list;
  }

  function summarySection(x, info) {
    const injected = injectedOptions(x);
    let line;
    if (info.kind === "closed") {
      line = "서버가 답하지 않고 연결을 끊었습니다." + (injected.length ? " 넣은 에러: " + injected.join(", ") : "");
    } else if (info.kind === "raw") {
      line = "설정에 직접 적어 둔 응답을 그대로 줬습니다.";
    } else if (info.kind === "injected") {
      line = "설정으로 넣은 에러입니다: " + injected.join(", ");
    } else if (info.kind === "rejected") {
      line = "요청이 규칙에 맞지 않아 서버가 거절했습니다." +
        (injected.length ? " 넣은 에러는 없고 늦게만 답했습니다: " + injected.join(", ") : " 넣은 에러는 없습니다.");
    } else {
      line = "정상 응답입니다." + (injected.length ? " 늦게만 답했습니다: " + injected.join(", ") : "");
    }
    const tool = UI.toolName(x);
    const duration = el("span", { text: durationText(x) + " ms" });
    const facts = el(
      "p",
      { class: "d-facts" },
      "#" + x.seq + " · " + (x.httpMethod || "") + " " + (x.path || "/mcp") + " · " +
        (x.method || "(메서드 없음)") + (tool ? " " + tool : "") + " · HTTP " + statusText(x) + " · ",
      duration
    );
    const node = section(
      null,
      el("div", { class: "d-summary" }, badge(info.kind, info.label), el("span", { text: line })),
      facts,
      el("p", { class: "hint", text: (UI.formatLocalStamp(x.timestamp) || x.time || "") + " · " + (x.timestamp || "") })
    );

    const body = x.responseBody;
    const error = body && typeof body === "object" && !Array.isArray(body) ? body.error : undefined;
    if (error && typeof error === "object") {
      const box = el("div", { class: "reason" + (info.kind === "rejected" ? "" : " neutral") });
      box.append(el("h3", { text: info.kind === "rejected" ? "거절한 이유" : "응답에 담긴 에러" }));
      if (typeof error.code === "number") {
        box.append(el("strong", { text: codeText({ code: error.code, codeName: info.codeName }) }));
        const description = codeDescription(error.code);
        if (description) box.append(el("span", { class: "hint", text: description }));
      }
      if (error.message !== undefined) box.append(el("span", { text: String(error.message) }));
      if (error.data !== undefined) {
        box.append(el("span", { class: "event-title", text: "error.data" }), el("pre", { text: pretty(error.data) }));
      }
      node.append(box);
    } else if (info.kind === "rejected") {
      node.append(el(
        "div",
        { class: "reason" },
        el("h3", { text: "거절한 이유" }),
        el("strong", { text: "HTTP " + x.status }),
        el("span", { text: typeof error === "string" ? error : "응답 바디에 이유가 없습니다." })
      ));
    }
    return { node: node, duration: duration };
  }

  function configSection(x) {
    const node = section("이 요청에 쓰인 설정");
    node.append(el("p", { text: "설정이 온 곳: " + (CONFIG_SOURCE_LABELS[x.configSource] || "알 수 없음 (설정을 읽기 전에 끝난 요청)") }));
    node.append(el("p", {
      class: "hint",
      text: "요청 방식: " + (ERA_SHORT[UI.requestMode(x)] || MODE_UNKNOWN) + " · 서버가 지원하는 스펙: " +
        (x.serverEra ? SERVER_ERA_LABELS[x.serverEra] + " (" + x.serverEra + ")" : ERA_UNKNOWN) +
        (x.source ? " · 요청 보내기 화면에서 보낸 요청" : "")
    }));
    let token = x.configSource === "url" ? x.cfg : null;
    if (x.configSource === "header") {
      const headers = x.requestHeaders || {};
      const name = Object.keys(headers).filter((key) => key.toLowerCase() === UI.SCENARIO_HEADER.toLowerCase())[0];
      const value = name ? String(headers[name]) : "";
      node.append(el("p", { class: "token", text: UI.SCENARIO_HEADER + ": " + value }));
      if (/^cfg:/i.test(value.trim())) token = value.trim().slice(4);
    }
    if (token) {
      const summary = summaryOf(token);
      if (!summary.ok) node.append(el("p", { class: "error-text", text: "설정 코드를 읽지 못했습니다." }));
      else if (summary.chips.length) node.append(el("p", { class: "hint", text: "바꾼 설정" }), chipList(summary));
      else node.append(el("p", { class: "hint", text: "기본 설정과 같습니다." }));
      const actions = el(
        "div",
        { class: "d-actions" },
        el("a", { href: "/scenario#cfg=" + encodeURIComponent(token), text: "서버 설정에서 열기" })
      );
      if (x.configSource === "url" && filters.cfg !== token) {
        const only = el("button", { type: "button", class: "secondary small", text: "이 주소로 온 요청만 보기" });
        only.addEventListener("click", () => { setCfg(token); });
        actions.append(only);
      }
      node.append(actions);
    }
    return node;
  }

  function compareSection(x) {
    const rows = UI.headerBodyRows(x, META.headerParams);
    if (!rows.length) return null;
    const body = el("tbody");
    rows.forEach((row) => {
      const headerCell = el("td", { text: row.headerValue === null ? "(없음)" : row.headerValue });
      if (row.decodedValue !== null) headerCell.append(el("span", { class: "decoded", text: "디코드: " + row.decodedValue }));
      body.append(el(
        "tr",
        null,
        el("th", { scope: "row", text: row.header }),
        headerCell,
        el("td", { text: row.bodyLabel }),
        el("td", { text: row.bodyValue === null ? "(없음)" : row.bodyValue }),
        el("td", null, el("span", { class: "badge " + (row.state === "match" ? "kind-ok" : "kind-rejected"), text: row.stateLabel }))
      ));
    });
    return section(
      "헤더와 바디 비교",
      el("p", { class: "hint", text: "2026 방식은 헤더와 바디에 같은 값을 실어야 합니다." }),
      el(
        "table",
        { class: "kv-table compare" },
        el("thead", null, el("tr", null,
          el("th", { scope: "col", text: "헤더" }), el("th", { scope: "col", text: "헤더 값" }),
          el("th", { scope: "col", text: "바디 위치" }), el("th", { scope: "col", text: "바디 값" }),
          el("th", { scope: "col", text: "비교" }))),
        body
      )
    );
  }

  function writeSecrets(x) {
    if (!detailRefs || detailRefs.seq !== x.seq) return;
    const headers = x.requestHeaders || {};
    detailRefs.requestHeaders.replaceChildren(
      headerTable(revealSecrets ? headers : UI.maskSecretHeaders(headers), "기록된 요청 헤더가 없습니다.")
    );
    const curl = UI.buildExchangeCurl(x, location.origin, revealSecrets);
    detailRefs.curl.textContent = curl.command;
    detailRefs.curlNote.textContent = curl.placeholders.length
      ? curl.placeholders.join(", ") + " 값은 가렸습니다. '값 보기'를 누른 뒤 복사하면 실제 값이 들어갑니다."
      : "";
    if (detailRefs.reveal) {
      detailRefs.reveal.setAttribute("aria-pressed", revealSecrets ? "true" : "false");
      detailRefs.reveal.textContent = revealSecrets ? "값 가리기" : "값 보기";
    }
  }

  function requestSection(x) {
    const headers = x.requestHeaders || {};
    const masked = UI.maskSecretHeaders(headers);
    const hasSecret = Object.keys(headers).some((name) => masked[name] !== headers[name]);
    const holder = el("div");
    let reveal = null;
    const headActions = el("div", { class: "d-actions" });
    if (hasSecret) {
      reveal = el("button", { type: "button", class: "secondary small", "aria-pressed": "false", text: "값 보기" });
      reveal.addEventListener("click", () => {
        revealSecrets = !revealSecrets;
        writeSecrets(x);
      });
      headActions.append(reveal);
    }
    const hasBody = x.requestBody !== null && x.requestBody !== undefined;
    const bodyActions = el("div", { class: "d-actions" });
    if (hasBody) bodyActions.append(copyButton("요청 바디 복사", "요청 바디", () => pretty(x.requestBody)));
    const node = section(
      null,
      el("div", { class: "d-head" }, el("h3", { text: "요청" }), headActions),
      el("p", { class: "d-facts", text: (x.httpMethod || "") + " " + (x.path || "/mcp") + (x.query ? "?" + x.query : "") }),
      holder,
      el("p", {
        class: "hint",
        text: "Accept, Content-Type, Authorization과 Mcp-, X-Mcp-로 시작하는 헤더만 남깁니다."
      }),
      el("div", { class: "d-head" }, el("span", { class: "event-title", text: "바디" }), bodyActions),
      hasBody ? el("pre", { tabindex: "0", text: pretty(x.requestBody) }) : el("p", { class: "hint", text: "바디가 없는 요청입니다." })
    );
    return { node: node, holder: holder, reveal: reveal };
  }

  function responseSection(x) {
    const body = x.responseBody;
    const hasBody = body !== null && body !== undefined;
    const actions = el("div", { class: "d-actions" });
    if (hasBody) actions.append(copyButton("응답 바디 복사", "응답 바디", () => pretty(body)));
    const node = section(
      null,
      el("div", { class: "d-head" }, el("h3", { text: "응답" }), actions),
      el("p", { class: "d-facts", text: x.status ? "HTTP " + x.status : "응답 없음 (연결 종료)" }),
      headerTable(x.responseHeaders, "남긴 응답 헤더가 없습니다.")
    );
    const events = hasBody && typeof body === "object" && Array.isArray(body.events) ? body.events : null;
    if (events) {
      node.append(el("p", { class: "hint", text: "SSE 응답: 이벤트 " + events.length + "개" }));
      events.forEach((event, index) => {
        node.append(
          el("span", { class: "event-title", text: "이벤트 " + (index + 1) + " / " + events.length }),
          el("pre", { tabindex: "0", text: pretty(event) })
        );
      });
    } else if (hasBody) {
      node.append(el("pre", { tabindex: "0", text: pretty(body) }));
    } else {
      node.append(el("p", { class: "hint", text: "바디가 없는 응답입니다." }));
    }
    return node;
  }

  function renderDetail(x) {
    const info = UI.classifyExchange(x, ERROR_CODES);
    const summary = summarySection(x, info);
    const request = requestSection(x);
    const curl = el("pre", { tabindex: "0" });
    const curlNote = el("p", { class: "hint" });
    const curlSection = section(
      null,
      el(
        "div",
        { class: "d-head" },
        el("h3", { text: "curl로 다시 보내기" }),
        el("div", { class: "d-actions" }, copyButton("curl 명령 복사", "curl 명령", () => curl.textContent))
      ),
      curl,
      curlNote
    );
    detailRefs = {
      seq: x.seq,
      duration: summary.duration,
      requestHeaders: request.holder,
      reveal: request.reveal,
      curl: curl,
      curlNote: curlNote
    };
    const sections = [summary.node, configSection(x), compareSection(x), request.node, responseSection(x), curlSection];
    byId("detailBody").replaceChildren(...sections.filter(Boolean));
    writeSecrets(x);
  }

  function markSelected(seq) {
    store.forEach((record, key) => {
      if (key === seq) record.row.setAttribute("aria-current", "true");
      else record.row.removeAttribute("aria-current");
    });
  }

  function openDetail(seq, byPointer) {
    const record = store.get(seq);
    if (!record) return;
    if (selectedSeq === seq && !byId("detail").hidden) return;
    selectedSeq = seq;
    selectedExchange = record.x;
    revealSecrets = false;
    markSelected(seq);
    byId("workspace").classList.add("has-detail");
    byId("detail").hidden = false;
    setText("detailTitle", "요청 상세 #" + seq);
    setText("detailStatus", "");
    renderDetail(record.x);
    const panel = byId("detail");
    panel.scrollTop = 0;
    // On a narrow screen the detail sits below the list: bring it into view.
    if (byPointer && panel.getBoundingClientRect && panel.getBoundingClientRect().top > window.innerHeight - 120) {
      panel.scrollIntoView({ block: "start" });
    }
    setText("announce", "요청 #" + seq + " 상세를 열었습니다. " + record.info.label + ".");
  }

  function closeDetail() {
    const seq = selectedSeq;
    selectedSeq = null;
    selectedExchange = null;
    detailRefs = null;
    markSelected(null);
    byId("detail").hidden = true;
    byId("workspace").classList.remove("has-detail");
    byId("detailBody").replaceChildren();
    setText("announce", "상세를 닫았습니다.");
    if (seq !== null && store.has(seq) && !store.get(seq).row.hidden) focusRow(seq);
  }

  // ---- wiring ------------------------------------------------------------

  function bindFilter(id, eventName, read) {
    byId(id).addEventListener(eventName, () => {
      read(byId(id));
      filtersChanged();
    });
  }

  KNOWN_METHODS.forEach(addMethodOption);

  bindFilter("fText", "input", (node) => { filters.text = node.value; });
  bindFilter("fKind", "change", (node) => { filters.kind = node.value; });
  bindFilter("fStatus", "change", (node) => { filters.status = node.value; });
  bindFilter("fEra", "change", (node) => { filters.era = node.value; });
  bindFilter("fMethod", "change", (node) => { filters.method = node.value; });
  bindFilter("fHideScreen", "change", (node) => { filters.hideScreen = node.checked; });
  bindFilter("fHideOptions", "change", (node) => { filters.hideOptions = node.checked; });
  bindFilter("fErrorsOnly", "change", (node) => { filters.errorsOnly = node.checked; });
  byId("applyCfg").addEventListener("click", applyCfgInput);
  byId("fCfg").addEventListener("keydown", (event) => { if (event.key === "Enter") applyCfgInput(); });
  byId("fCfg").addEventListener("paste", () => { setTimeout(applyCfgInput, 0); });
  byId("scopeAll").addEventListener("click", () => { setScope("", false); });
  byId("scopePlain").addEventListener("click", () => { setScope("", true); });
  byId("resetFilters").addEventListener("click", resetFilters);
  byId("emptyReset").addEventListener("click", resetFilters);
  byId("emptyShow").addEventListener("click", () => { releaseFilters(blockingKeys); });
  byId("newBannerReset").addEventListener("click", () => {
    // Turn off the filters that hide the new requests. Nothing else changes.
    const keys = byId("newBanner").blockers || [];
    if (keys.length) releaseFilters(keys);
    else resetFilters();
  });

  byId("refreshBtn").addEventListener("click", () => { poll(true); });
  byId("hideBtn").addEventListener("click", () => {
    if (!lastSeq) return;
    hideBefore = lastSeq;
    applyFilters();
    setText("announce", "지금까지의 기록을 숨겼습니다.");
  });
  byId("unhideBtn").addEventListener("click", () => {
    hideBefore = 0;
    applyFilters();
    setText("announce", "숨긴 기록을 다시 표시했습니다.");
  });
  byId("closeDetail").addEventListener("click", closeDetail);
  byId("detail").addEventListener("keydown", (event) => { if (event.key === "Escape") closeDetail(); });

  liveToggle.checked = true;
  liveToggle.addEventListener("change", () => {
    setText("announce", liveToggle.checked ? "자동 새로고침을 켰습니다." : "자동 새로고침을 껐습니다.");
    if (liveToggle.checked) poll();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

  writeFilterControls();
  writeCfgSummary();
  // A link that carries detailed filters opens with them in view.
  if (writeFilterCount() > 0) byId("filterBox").open = true;
  setInterval(tick, POLL_MS);
  poll();
})();
"""
# ---------------------------------------------------------------------------
# /send page: builds one /mcp request from a form or from pasted text (a
# request bundle, a JSON-RPC body or a curl command), sends it and shows the
# response. Requests it sends are recorded with INSPECT_CONSOLE_SOURCE.
# ---------------------------------------------------------------------------
# Arguments the form fills in when a known tool is chosen.
SEND_TOOL_EXAMPLES: dict[str, dict[str, Any]] = {
    "convert_length": {"value": 1, "from_unit": "m", "to_unit": "cm"},
    "convert_weight": {"value": 1, "from_unit": "kg", "to_unit": "g"},
    "convert_temperature": {"value": 25, "from_unit": "c", "to_unit": "f"},
    "convert_area": {"value": 1, "from_unit": "m2", "to_unit": "cm2"},
    "convert_volume": {"value": 1, "from_unit": "l", "to_unit": "ml"},
    "list_supported_units": {},
    HEADER_PARAMS_TOOL: {"region": "kr", "greeting": "hello", "count": 3},
    MRTR_TOOL: {"action": "delete"},
}
SEND_METHODS = ("tools/list", "tools/call", "server/discover", "initialize", "ping", "subscriptions/listen")


def render_send_page() -> str:
    meta = scenario_meta()
    tool_names = [tool["name"] for tool in tools()] + [tool["name"] for tool in modern_test_tools()]
    page_meta = {
        "defaults": meta["defaults"],
        "options": _summary_options(meta),
        "tools": tool_names,
        "toolArgs": {name: SEND_TOOL_EXAMPLES.get(name, {}) for name in tool_names},
        "headerParams": _header_param_map(),
        "consoleSource": INSPECT_CONSOLE_SOURCE,
        "sourceHeader": INSPECT_SOURCE_HEADER,
        "modernVersion": PROTOCOL_VERSION_2026,
    }
    methods = "".join(f'<option value="{escape(name)}"></option>' for name in SEND_METHODS)
    tool_options = "".join(f'<option value="{escape(name)}"></option>' for name in tool_names)
    return _fill_template(
        _SEND_PAGE,
        {
            "BASE_CSS": UI_BASE_CSS,
            "NAV_CSS": UI_NAV_CSS,
            "NAV": render_nav("send"),
            "METHODS": methods,
            "TOOLS": tool_options,
            "FORMAT_ITEM": render_copy_item("requestFormat", "요청 JSON 형식 (AI에게 주고 요청을 만들게 할 때)", "요청 JSON 형식"),
            "BUNDLE_ITEM": render_copy_item("bundle", "지금 요청을 JSON으로", "지금 요청 JSON"),
            "CURL_ITEM": render_copy_item("curl", "지금 요청을 curl로", "curl 명령"),
            "UI_CORE_JS": UI_CORE_JS,
            "PAGE_JS": _fill_template(_SEND_PAGE_JS, {"META_JSON": _script_json(page_meta)}),
        },
    )


_SEND_PAGE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unit Expert MCP · 요청 보내기</title>
  <style>
    __BASE_CSS__
    __NAV_CSS__
    pre.response {
      margin: 0; padding: 10px 12px; border-radius: 6px; background: #101818; color: #d9f2ed;
      white-space: pre-wrap; overflow-wrap: anywhere; max-height: 70vh; overflow: auto; line-height: 1.5; min-height: 120px;
    }
    .send-layout { margin-top: 16px; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 20px; align-items: start; }
    .stack { display: grid; gap: 14px; min-width: 0; }
    .field { display: grid; gap: 4px; min-width: 0; align-content: start; }
    .field label, .field-title { font-size: 13px; font-weight: 600; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .mono-input { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px !important; }
    .check { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    #sendBtn { padding: 9px 26px; font-size: 14px; }
    .paste { background: var(--soft); border-color: var(--soft-line); }
    .fold { border-top: 1px solid var(--line); padding-top: 10px; }
    .fold > summary { cursor: pointer; font-weight: 600; font-size: 13px; color: var(--accent-strong); }
    .fold-body { display: grid; gap: 12px; margin-top: 10px; }
    [hidden] { display: none !important; }
    .note { border-radius: 8px; padding: 9px 12px; background: #f4f6fa; font-size: 13px; overflow-wrap: anywhere; }
    .note:empty, pre.response:empty { display: none; }
    .note.ok { background: #e7f6ee; color: var(--success); }
    .note.err { background: var(--danger-soft); color: var(--danger); }
    .result-title { font-size: 20px; font-weight: 700; }
    .metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 0; }
    .metric { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
    .metric dt { color: var(--muted); font-size: 11px; font-weight: 600; }
    .metric dd { margin: 0; font-size: 18px; font-weight: 700; }
    .response-card { position: sticky; top: 12px; }
    @media (max-width: 980px) {
      .send-layout { grid-template-columns: minmax(0, 1fr); }
      .response-card { position: static; }
    }
    @media (max-width: 620px) { .grid-2, .metrics { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  __NAV__
  <main>
    <h1>요청 보내기</h1>
    <p class="lead">MCP 주소로 요청을 보내고 응답을 봅니다.</p>
    <div class="send-layout">
      <div class="stack">
        <section class="card paste stack" aria-labelledby="pasteTitle">
          <h2 id="pasteTitle" style="margin:0"><label for="pasteBox">붙여 넣어 채우기</label></h2>
          <textarea id="pasteBox" rows="3" class="mono-input" spellcheck="false" autocomplete="off" placeholder="요청 JSON, JSON-RPC 바디, curl 명령 중 하나를 붙여 넣으세요"></textarea>
          <div class="actions">
            <button type="button" class="secondary" id="pasteApply">채우기</button>
            <span id="pasteStatus" class="status-line" aria-live="polite"></span>
          </div>
          <p id="pasteError" class="error-text" role="alert"></p>
          <details class="fold" id="formatBox">
            <summary>AI에게 요청 만들게 하기 · 지금 요청 복사</summary>
            <div class="fold-body">
              __FORMAT_ITEM__
              __BUNDLE_ITEM__
              __CURL_ITEM__
              <p id="copyStatus" class="status-line" aria-live="polite"></p>
            </div>
          </details>
        </section>

        <section class="card stack" aria-labelledby="formTitle">
          <div class="card-head" style="margin:0">
            <h2 id="formTitle">요청</h2>
            <div class="field">
              <label for="preset" class="sr-only">예시 요청</label>
              <select id="preset">
                <option value="">예시에서 고르기</option>
                <option value="tools-list">툴 목록 (2026 방식)</option>
                <option value="tools-call">툴 호출 (2026 방식)</option>
                <option value="discover">서버 정보 (2026 방식)</option>
                <option value="legacy-tools-list">툴 목록 (2025 방식)</option>
                <option value="legacy-tools-call">툴 호출 (2025 방식)</option>
                <option value="missing-meta-version">틀린 요청: 바디에 버전 없음</option>
                <option value="header-mismatch">틀린 요청: 헤더와 바디가 다름</option>
                <option value="subscriptions-listen">subscriptions/listen</option>
                <option value="tools-list-cache">툴 목록 캐시 확인</option>
              </select>
            </div>
          </div>
          <div class="field">
            <label for="cUrl">MCP 주소</label>
            <input id="cUrl" type="text" class="mono-input" value="/mcp" spellcheck="false" autocomplete="off">
            <ul id="urlChips" class="chips" aria-label="이 주소에서 바꾼 설정"></ul>
          </div>
          <div class="grid-2">
            <div class="field">
              <label for="cMode">요청 방식</label>
              <select id="cMode">
                <option value="2026">2026 방식 (요청마다 버전을 실음)</option>
                <option value="2025">2025 방식 (initialize로 시작)</option>
              </select>
            </div>
            <div class="field">
              <label for="cMethod">메서드</label>
              <input id="cMethod" type="text" list="methodList" value="tools/list" spellcheck="false" autocomplete="off">
              <datalist id="methodList">__METHODS__</datalist>
            </div>
          </div>
          <label class="check" id="initRow" hidden><input type="checkbox" id="cInit" checked> 먼저 initialize를 보내 세션을 받습니다</label>
          <div class="stack" id="callFields" hidden>
            <div class="field">
              <label for="cTool">툴</label>
              <input id="cTool" type="text" list="toolList" value="convert_length" spellcheck="false" autocomplete="off">
              <datalist id="toolList">__TOOLS__</datalist>
            </div>
            <div class="field">
              <label for="cArgs">인자 (JSON)</label>
              <textarea id="cArgs" rows="3" class="mono-input" spellcheck="false" autocomplete="off"></textarea>
              <p id="argsError" class="error-text" role="alert"></p>
            </div>
          </div>
          <details class="fold" id="rawBox">
            <summary>보낼 헤더와 바디 직접 고치기<span id="rawEdited" class="hint" hidden> · 고친 내용이 있습니다</span></summary>
            <div class="fold-body">
              <div class="field">
                <label for="hdrBox">헤더</label>
                <textarea id="hdrBox" class="mono-input" spellcheck="false" rows="5"></textarea>
              </div>
              <div class="field">
                <label for="bodyBox">바디</label>
                <textarea id="bodyBox" class="mono-input" spellcheck="false" rows="12"></textarea>
              </div>
              <div class="actions"><button class="secondary small" id="fillBtn" type="button">위 칸의 내용으로 되돌리기</button></div>
            </div>
          </details>
          <div class="actions">
            <button id="sendBtn" type="button">보내기</button>
          </div>
          <details class="fold" id="cacheBox">
            <summary>캐시 확인</summary>
            <div class="fold-body">
              <label class="check"><input type="checkbox" id="clientCacheEnabled"> 응답의 ttlMs 동안 같은 툴 목록 요청을 다시 보내지 않기</label>
              <div class="actions"><button class="secondary" id="sendTwiceBtn" type="button">2번 이어서 보내기</button></div>
              <dl class="metrics">
                <div class="metric"><dt>보내기 누름</dt><dd id="metricRuns">0</dd></div>
                <div class="metric"><dt>서버로 보냄</dt><dd id="metricHits">0</dd></div>
                <div class="metric"><dt>캐시 사용</dt><dd id="metricCache">0</dd></div>
              </dl>
            </div>
          </details>
        </section>
      </div>

      <section class="card stack response-card" aria-labelledby="responseTitle">
        <h2 id="responseTitle" style="margin:0">응답</h2>
        <div>
          <div class="result-title" id="resultTitle">아직 보내지 않았습니다</div>
          <div class="hint" id="resultText"></div>
        </div>
        <div class="note" id="callNote" role="status" aria-live="polite"></div>
        <pre id="lastResponse" class="response" tabindex="0" aria-label="응답 내용"></pre>
        <p><a id="inspectLink" href="/inspect?screen=1" hidden>요청 기록에서 보기</a></p>
      </section>
    </div>
  </main>
  <script>
__UI_CORE_JS__
  </script>
  <script>
__PAGE_JS__
  </script>
</body>
</html>"""


_SEND_PAGE_JS = r"""(function () {
  "use strict";
  const UI = UnitExpertUI;
  const META = __META_JSON__;
  const DEFAULTS = META.defaults;
  const SCHEMA = META.options;
  const byId = (id) => document.getElementById(id);
  const cUrl = byId("cUrl");
  const cMode = byId("cMode");
  const cMethod = byId("cMethod");
  const cTool = byId("cTool");
  const cArgs = byId("cArgs");
  const cInit = byId("cInit");
  const hdrBox = byId("hdrBox");
  const bodyBox = byId("bodyBox");
  const sendBtn = byId("sendBtn");
  const sendTwiceBtn = byId("sendTwiceBtn");
  const clientCacheEnabled = byId("clientCacheEnabled");
  const SEND_TIMEOUT_MS = 30000;
  // A config whose tools/list answer may be cached for a minute.
  const CACHE_TARGET = "/mcp?cfg=" + UI.encodeToken(
    UI.applyPatch(DEFAULTS, { toolsList: { cacheTtlMs: 60000 } }), DEFAULTS, SCHEMA
  );
  const EXAMPLES = {
    "tools-list": { mode: "2026", method: "tools/list" },
    "tools-call": { mode: "2026", method: "tools/call", tool: "convert_length", args: META.toolArgs.convert_length },
    "discover": { mode: "2026", method: "server/discover" },
    "legacy-tools-list": { mode: "2025", method: "tools/list" },
    "legacy-tools-call": { mode: "2025", method: "tools/call", tool: "convert_length", args: META.toolArgs.convert_length },
    "missing-meta-version": { mode: "2026", method: "tools/list", omit: ["meta-version"] },
    "header-mismatch": { mode: "2026", method: "tools/list", headers: { "Mcp-Method": "tools/call" } },
    "subscriptions-listen": { mode: "2026", method: "subscriptions/listen" },
    "tools-list-cache": { url: CACHE_TARGET, mode: "2026", method: "tools/list" }
  };

  // What a pasted request holds that the form has no field for. It stays
  // until "위 칸의 내용으로 되돌리기".
  let extra = { headers: null, omit: [], params: null, id: undefined };
  // The headers and body the form alone makes: what the two boxes are compared with.
  let draft = { headers: {}, bodyText: "" };
  let copyValues = {};
  let sending = false;
  let addressBeforeExample = null;
  const clientCache = new Map();
  let runCount = 0;
  let serverHitCount = 0;
  let cacheHitCount = 0;

  function setText(id, text) {
    const node = byId(id);
    if (node.textContent !== text) node.textContent = text;
  }

  function pretty(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch (error) { return String(value); }
  }

  function readJson(text) {
    try { return JSON.parse(text); } catch (error) { return null; }
  }

  function headerText(headers) {
    return Object.keys(headers).map((name) => name + ": " + headers[name]).join("\n");
  }

  function parseHeaders(text) {
    const headers = {};
    text.split("\n").forEach((line) => {
      const trimmed = line.trim();
      const index = trimmed.indexOf(":");
      if (!trimmed || index === -1) return;
      const key = trimmed.slice(0, index).trim();
      if (key) headers[key] = trimmed.slice(index + 1).trim();
    });
    return headers;
  }

  /* The arguments field as an object. undefined when it is not a JSON object. */
  function readArgs() {
    const text = cArgs.value.trim();
    if (!text) {
      setText("argsError", "");
      return {};
    }
    try {
      const value = JSON.parse(text);
      if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("JSON 객체({ … })여야 합니다");
      setText("argsError", "");
      return value;
    } catch (error) {
      setText("argsError", /객체/.test(error.message) ? error.message : UI.jsonErrorLine(text, error));
      return undefined;
    }
  }

  function formRequest() {
    const method = cMethod.value.trim();
    const isCall = method === "tools/call";
    return {
      url: cUrl.value.trim(),
      mode: cMode.value,
      method: method,
      tool: isCall ? cTool.value.trim() : "",
      args: isCall ? readArgs() : null,
      params: extra.params,
      id: extra.id,
      headers: extra.headers,
      omit: extra.omit
    };
  }

  function syncFields() {
    const isCall = cMethod.value.trim() === "tools/call";
    byId("callFields").hidden = !isCall;
    byId("initRow").hidden = cMode.value !== "2025" || cMethod.value.trim() === "initialize";
  }

  function draftOptions() {
    return { clientName: META.consoleSource, headerParams: META.headerParams };
  }

  /* Fills the header and body boxes from the form. */
  function writeDraft() {
    syncFields();
    const request = formRequest();
    if (request.args === undefined || !request.method) return;
    const made = UI.buildRequestDraft(request, draftOptions());
    draft = { headers: made.headers, bodyText: made.bodyText };
    hdrBox.value = headerText(made.headers);
    bodyBox.value = made.bodyText;
    refresh();
  }

  function rawEdited() {
    return hdrBox.value.trim() !== headerText(draft.headers) || bodyBox.value.trim() !== draft.bodyText.trim();
  }

  function absoluteUrl(target) {
    try { return new URL(target || "/mcp", location.origin).href; } catch (error) { return target; }
  }

  /* What depends on the form and the two boxes: the address summary and the
     texts of the copy buttons. */
  function refresh() {
    const token = UI.tokenFromText(cUrl.value.indexOf("cfg=") >= 0 ? cUrl.value : "");
    const chips = byId("urlChips");
    chips.replaceChildren();
    if (token) {
      const summary = UI.tokenSummary(token, DEFAULTS, SCHEMA);
      (summary.ok ? summary.chips : []).forEach((chip) => {
        const item = document.createElement("li");
        item.className = "chip" + (chip.effective ? "" : " is-ineffective");
        item.textContent = chip.label + " = " + chip.value;
        chips.append(item);
      });
    }
    byId("rawEdited").hidden = !rawEdited();
    const request = formRequest();
    const form = {
      url: request.url, mode: request.mode, method: request.method, tool: request.tool,
      args: request.args || null, id: extra.id
    };
    const headers = parseHeaders(hdrBox.value);
    const curlLines = ["curl -i -sS -X POST '" + absoluteUrl(request.url).replace(/'/g, "'\\''") + "'"];
    Object.keys(headers).forEach((name) => {
      curlLines.push("-H '" + (name + ": " + headers[name]).replace(/'/g, "'\\''") + "'");
    });
    const compactBody = readJson(bodyBox.value);
    curlLines.push("-d '" + (compactBody === null ? bodyBox.value : JSON.stringify(compactBody)).replace(/'/g, "'\\''") + "'");
    copyValues = {
      requestFormat: UI.requestFormatText(),
      bundle: JSON.stringify(UI.requestBundle(form, headers, bodyBox.value, draftOptions()), null, 2),
      curl: curlLines.join(" \\\n  ")
    };
    Object.keys(copyValues).forEach((key) => {
      const node = byId("copy-" + key);
      if (node.textContent !== copyValues[key]) node.textContent = copyValues[key];
    });
    syncExpandButtons();
  }

  function syncExpandButtons() {
    document.querySelectorAll("[data-expand]").forEach((button) => {
      const text = byId(button.dataset.expand);
      const open = text.classList.contains("is-open");
      button.hidden = !open && !(text.scrollHeight > text.clientHeight + 1);
      button.setAttribute("aria-expanded", open ? "true" : "false");
      button.textContent = open ? "접기" : "전체 보기";
    });
  }

  /* Puts a parsed request (UI.parseRequestPaste) into the form. */
  function applyRequest(request) {
    if (request.url) cUrl.value = request.url;
    cMode.value = request.mode;
    cMethod.value = request.method || cMethod.value;
    if (request.method === "tools/call") {
      cTool.value = request.tool || "";
      cArgs.value = JSON.stringify(request.args || {}, null, 2);
    }
    extra = {
      headers: request.exactHeaders ? null : request.headers,
      omit: request.omit || [],
      params: request.params,
      id: request.id
    };
    writeDraft();
    // A body or a header list given as it is replaces what the form made.
    const exact = UI.buildRequestDraft(request, draftOptions());
    if (request.exactHeaders) hdrBox.value = headerText(exact.headers);
    if (request.body !== undefined || request.bodyText !== undefined) bodyBox.value = exact.bodyText;
    if (rawEdited()) byId("rawBox").open = true;
    refresh();
  }

  const PASTE_KINDS = { bundle: "요청 JSON을", jsonrpc: "JSON-RPC 바디를", curl: "curl 명령을" };

  function applyPaste() {
    const parsed = UI.parseRequestPaste(byId("pasteBox").value);
    setText("pasteError", parsed.ok ? "" : parsed.error);
    setText("pasteStatus", "");
    if (!parsed.ok) return false;
    applyRequest(parsed.request);
    setText("pasteStatus", PASTE_KINDS[parsed.kind] + " 읽어 아래 칸을 채웠습니다.");
    return true;
  }

  function applyExample() {
    const name = byId("preset").value;
    if (!name) return;
    clientCacheEnabled.checked = name === "tools-list-cache";
    if (name === "tools-list-cache") byId("cacheBox").open = true;
    // The cache example brings its own address. Leaving it puts the user's back.
    if (EXAMPLES[name].url && addressBeforeExample === null) addressBeforeExample = cUrl.value;
    if (!EXAMPLES[name].url && addressBeforeExample !== null) {
      cUrl.value = addressBeforeExample;
      addressBeforeExample = null;
    }
    applyRequest(UI.parseRequestPaste(JSON.stringify(EXAMPLES[name])).request);
  }

  function setNote(kind, text) {
    const note = byId("callNote");
    note.className = ("note " + (kind || "")).trim();
    note.textContent = text;
  }

  function normalizedCacheKey(target, headers, payload) {
    if (!payload || payload.method !== "tools/list") return "";
    const params = payload.params && typeof payload.params === "object"
      ? JSON.parse(JSON.stringify(payload.params))
      : {};
    return JSON.stringify({
      target: target,
      protocolVersion: UI.headerValue(headers, "MCP-Protocol-Version"),
      method: payload.method,
      params: params
    });
  }

  function updateMetrics() {
    setText("metricRuns", String(runCount));
    setText("metricHits", String(serverHitCount));
    setText("metricCache", String(cacheHitCount));
  }

  function setResult(title, text, response) {
    setText("resultTitle", title);
    setText("resultText", text);
    byId("lastResponse").textContent = response === undefined ? "" : pretty(response);
  }

  async function post(target, headers, body) {
    // A config that delays or never answers must not leave the page waiting.
    const controller = new AbortController();
    const timer = setTimeout(() => { controller.abort(); }, SEND_TIMEOUT_MS);
    try {
      const response = await fetch(target, { method: "POST", headers: headers, body: body, signal: controller.signal });
      const text = await response.text();
      return { status: response.status, ok: response.ok, text: text, sessionId: response.headers.get("mcp-session-id") };
    } catch (error) {
      if (controller.signal.aborted) throw new Error(SEND_TIMEOUT_MS / 1000 + "초 안에 응답이 오지 않아 그만 기다렸습니다");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  async function sendRequest() {
    const headers = parseHeaders(hdrBox.value);
    const rawBody = bodyBox.value;
    const target = cUrl.value.trim() || "/mcp";
    const parsedBody = readJson(rawBody);
    const cacheKey = clientCacheEnabled.checked ? normalizedCacheKey(target, headers, parsedBody) : "";
    const sameOrigin = target.startsWith("/") || target.startsWith(location.origin);
    runCount += 1;
    updateMetrics();

    if (cacheKey) {
      const cached = clientCache.get(cacheKey);
      if (cached && cached.expiresAt > Date.now()) {
        cacheHitCount += 1;
        updateMetrics();
        const seconds = Math.ceil((cached.expiresAt - Date.now()) / 1000);
        setNote("ok", "캐시에 있어 서버로 보내지 않았습니다. 남은 시간 " + seconds + "초");
        setResult("캐시 사용", "", cached.response);
        return;
      }
      clientCache.delete(cacheKey);
    }

    // Names this page as the sender in the request log. Only for this server.
    if (sameOrigin) headers[META.sourceHeader] = META.consoleSource;
    const steps = [];
    const legacyInit = cMode.value === "2025" && cInit.checked && !(parsedBody && parsedBody.method === "initialize") &&
      !UI.headerValue(headers, "Mcp-Session-Id");
    if (legacyInit) {
      // The way a 2025 client starts: initialize, then the request with the session it got.
      const init = UI.buildRequestDraft({ mode: "2025", method: "initialize", id: 0 }, draftOptions());
      const initHeaders = Object.assign({}, init.headers);
      if (sameOrigin) initHeaders[META.sourceHeader] = META.consoleSource;
      ["Authorization", "X-MCP-Test-Scenario", "X-Mock-Auth"].forEach((name) => {
        if (UI.headerValue(headers, name)) initHeaders[name] = UI.headerValue(headers, name);
      });
      serverHitCount += 1;
      const first = await post(target, initHeaders, init.bodyText);
      steps.push("initialize → HTTP " + first.status);
      if (first.sessionId) headers["Mcp-Session-Id"] = first.sessionId;
      const firstJson = readJson(first.text);
      const version = firstJson && firstJson.result ? firstJson.result.protocolVersion : null;
      if (typeof version === "string" && !UI.headerValue(headers, "MCP-Protocol-Version")) headers["MCP-Protocol-Version"] = version;
      if (!first.ok) {
        setNote("err", "initialize가 HTTP " + first.status + "로 끝나 요청을 보내지 않았습니다.");
        setResult("HTTP " + first.status, "initialize의 응답입니다.", firstJson || first.text);
        return;
      }
    }

    serverHitCount += 1;
    updateMetrics();
    const result = await post(target, headers, rawBody);
    const responseJson = readJson(result.text);
    const response = responseJson || result.text;
    const ttlMs = responseJson && responseJson.result ? Number(responseJson.result.ttlMs || 0) : 0;
    if (cacheKey && result.ok && ttlMs > 0) clientCache.set(cacheKey, { expiresAt: Date.now() + ttlMs, response: response });
    steps.push((parsedBody && parsedBody.method ? parsedBody.method : "요청") + " → HTTP " + result.status);
    const error = responseJson && responseJson.error;
    const isError = responseJson && responseJson.result && responseJson.result.isError === true;
    let verdict = "정상 응답입니다.";
    if (error) verdict = "에러 응답입니다: " + (error.code !== undefined ? error.code + " " : "") + (error.message || "");
    else if (!result.ok) verdict = "HTTP 에러입니다.";
    else if (isError) verdict = "툴이 isError로 답했습니다.";
    setNote(result.ok && !error && !isError ? "ok" : "err", verdict + (cacheKey && ttlMs > 0 ? " " + ttlMs + "ms 동안 캐시에 둡니다." : ""));
    setResult("HTTP " + result.status, steps.join(" · "), response || "(바디 없음)");
    const link = byId("inspectLink");
    link.hidden = !sameOrigin;
    const token = UI.tokenFromText(target.indexOf("cfg=") >= 0 ? target : "");
    link.href = "/inspect?" + (token ? "cfg=" + encodeURIComponent(token) + "&" : "plain=1&") + "screen=1";
  }

  async function runSend(count) {
    if (sending) return;
    sending = true;
    sendBtn.disabled = true;
    sendTwiceBtn.disabled = true;
    setNote("", "보내는 중입니다.");
    try {
      for (let index = 0; index < count; index += 1) await sendRequest();
    } catch (error) {
      setNote("err", "응답을 받지 못했습니다: " + (error && error.message ? error.message : error));
      setResult("응답 없음", "연결을 끊거나 답하지 않는 설정이면 정상입니다.", undefined);
    } finally {
      sending = false;
      updateMetrics();
      sendBtn.disabled = false;
      sendTwiceBtn.disabled = false;
    }
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.setAttribute("aria-hidden", "true");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    const done = document.execCommand("copy");
    area.remove();
    if (!done) throw new Error("copy failed");
  }

  document.addEventListener("click", async (event) => {
    const target = event.target.closest("button");
    if (!target) return;
    if (target.dataset.copy) {
      try {
        await copyText(copyValues[target.dataset.copy]);
        setText("copyStatus", "복사했습니다: " + target.dataset.copyLabel);
      } catch (error) {
        setText("copyStatus", "복사하지 못했습니다. 글자를 직접 선택해 복사하세요.");
      }
    } else if (target.dataset.expand) {
      byId(target.dataset.expand).classList.toggle("is-open");
      syncExpandButtons();
    }
  });

  // A change in the form makes the request again from the form.
  [cMode, cMethod].forEach((input) => { input.addEventListener("input", writeDraft); });
  cTool.addEventListener("input", () => {
    // A known tool brings arguments that work.
    const known = META.toolArgs[cTool.value.trim()];
    if (known) cArgs.value = JSON.stringify(known, null, 2);
    writeDraft();
  });
  cArgs.addEventListener("input", writeDraft);
  cUrl.addEventListener("input", refresh);
  [hdrBox, bodyBox].forEach((input) => { input.addEventListener("input", refresh); });
  byId("fillBtn").addEventListener("click", () => {
    extra = { headers: null, omit: [], params: null, id: undefined };
    writeDraft();
  });
  byId("preset").addEventListener("change", applyExample);
  byId("pasteApply").addEventListener("click", applyPaste);
  byId("pasteBox").addEventListener("paste", () => { setTimeout(applyPaste, 0); });
  byId("formatBox").addEventListener("toggle", syncExpandButtons);
  window.addEventListener("resize", syncExpandButtons);
  sendBtn.addEventListener("click", () => { runSend(1); });
  sendTwiceBtn.addEventListener("click", () => { runSend(2); });

  // /send?cfg=<code> (from the other screens) or ?url=<address> fills the address.
  const query = new URLSearchParams(location.search);
  if (query.get("url")) cUrl.value = query.get("url");
  else if (query.get("cfg")) cUrl.value = "/mcp?cfg=" + encodeURIComponent(query.get("cfg"));
  cArgs.value = JSON.stringify(META.toolArgs.convert_length, null, 2);
  writeDraft();
  updateMetrics();
})();
"""


def _advanced_field(
    path: str,
    kind: str,
    label: str,
    hint: str = "",
    options: Any = None,
) -> dict[str, Any]:
    return {"path": path, "kind": kind, "label": label, "hint": hint, "options": options}


def advanced_field_groups() -> tuple[tuple[str, str, list[dict[str, Any]]], ...]:
    """Config paths the /scenario page must have a control for, grouped by era.

    The page itself is rendered from scenario_meta(); tests check that every
    path listed here has a control (data-config-path) on it.
    """
    return (
        (
            "2026-07-28 전용",
            "모던(stateless) 경로에만 적용",
            [
                _advanced_field(
                    "strict2026", "bool", "스펙 준수 모드", "끄면 이전의 느슨한 2026 동작"
                ),
                _advanced_field(
                    "discover.mode", "select", "server/discover 응답", options=DISCOVER_MODES
                ),
                _advanced_field(
                    "discover.errorCode", "int", "discover JSON-RPC 에러 코드", "mode=json-rpc-error"
                ),
                _advanced_field(
                    "discover.httpStatus", "int", "discover HTTP 5xx 코드", "mode=http-5xx, 500~599"
                ),
                _advanced_field("discover.delaySeconds", "float", "discover 지연(초)", "0~120"),
                _advanced_field("discover.cacheTtlMs", "int", "discover ttlMs"),
                _advanced_field(
                    "discover.cacheScope",
                    "select",
                    "discover cacheScope",
                    options={"private": "private", "public": "public"},
                ),
                _advanced_field(
                    "modernResponse.resultType",
                    "select",
                    "resultType 위반 주입",
                    options=MODERN_RESULT_TYPE_MODES,
                ),
                _advanced_field(
                    "modernResponse.methods",
                    "list",
                    "위반 주입 대상 메서드",
                    "쉼표 구분, 비우면 전체 (" + ", ".join(MODERN_RESULT_METHODS) + ")",
                ),
                _advanced_field("modernResponse.omitServerInfo", "bool", "_meta serverInfo 누락"),
                _advanced_field("modernResponse.omitCacheFields", "bool", "ttlMs/cacheScope 누락"),
                _advanced_field(
                    "modernResponse.issueSessionId", "bool", "2026 응답에 Mcp-Session-Id 발급"
                ),
                _advanced_field(
                    "modern.testTools",
                    "bool",
                    "테스트 툴 노출",
                    f"{HEADER_PARAMS_TOOL}, {MRTR_TOOL}",
                ),
                _advanced_field(
                    "modern.requiredClientCapabilities",
                    "list",
                    "필수 클라이언트 capability (-32021)",
                    "쉼표 구분: " + ", ".join(CLIENT_CAPABILITY_NAMES),
                ),
                _advanced_field(
                    "modern.invalidParamsHttpStatus",
                    "select-int",
                    "-32602 HTTP 상태",
                    "알 수 없는 툴, 잘못된 커서 (스펙 미규정)",
                    options={"200": "200", "400": "400"},
                ),
                _advanced_field(
                    "modern.rejectUnexpectedParamHeader",
                    "bool",
                    "바디에 값이 없는 Mcp-Param-* 거부",
                    "스펙 미규정. 끄면 헤더를 무시",
                ),
            ],
        ),
        (
            "레거시 전용",
            "initialize 핸드셰이크 경로 / serverEra=legacy",
            [
                _advanced_field(
                    "legacyModernProbe.mode",
                    "select",
                    "legacy 서버가 2026 요청에 주는 응답",
                    "serverEra=legacy일 때만",
                    options=LEGACY_MODERN_PROBE_MODES,
                ),
                _advanced_field(
                    "legacyModernProbe.delaySeconds", "float", "timeout 모드 무응답 시간(초)", "0~120"
                ),
                _advanced_field(
                    "initialize.protocolVersion",
                    "select",
                    "initialize 응답 버전",
                    "최소 지원 버전 시나리오(2024-11-05)를 켜면 그 값이 우선",
                    options={
                        version: label
                        for version, label in PROTOCOL_VERSION_CHOICES
                        if version != "2024-11-05"
                    },
                ),
                _advanced_field("session.validate", "bool", "세션 검증", "누락 400, 모르는 세션 404, DELETE 종료"),
                _advanced_field("session.issueId", "bool", "세션 ID 발급"),
            ],
        ),
        (
            "공용",
            "두 era 모두 적용",
            [
                _advanced_field("toolCall.mode", "select", "tools/call 응답", options=TOOL_CALL_MODES),
                _advanced_field(
                    "toolCall.errorCode", "int", "tools/call JSON-RPC 에러 코드", "mode=json-rpc-error"
                ),
                _advanced_field(
                    "toolCall.httpStatus", "int", "tools/call HTTP 상태", "mode=http-status, 400~599"
                ),
                _advanced_field("toolCall.delaySeconds", "float", "tools/call 지연(초)", "0~120"),
                _advanced_field(
                    "toolCall.sseNotificationCount", "int", "SSE 알림 개수", "mode=sse, 0~20"
                ),
                _advanced_field(
                    "toolCall.targetTools", "list", "대상 툴", "쉼표 구분, 비우면 전체"
                ),
                _advanced_field("toolsList.pageSize", "int", "tools/list 페이지 크기", "0=페이지네이션 없음"),
                _advanced_field(
                    "toolsList.failAtPage", "int", "실패시킬 페이지 번호", "0=끔, pageSize>0일 때만"
                ),
                _advanced_field(
                    "allowedOrigins", "list", "Origin 허용 목록", "쉼표 구분, 비우면 모두 허용"
                ),
                _advanced_field(
                    "auth.enforceScope", "bool", "토큰 scope 검사", "mcp 스코프 없으면 403 insufficient_scope"
                ),
            ],
        ),
    )


# ---------------------------------------------------------------------------
# Screen code shared by /scenario and /inspect.
#
# UI_CORE_JS holds the screen logic that needs no DOM (tokens, config diff,
# option state, command strings, exchange classification). Both pages inline
# it, and tests/test_ui_logic.py runs it under node.
# ---------------------------------------------------------------------------
PREVIEW_SOURCE = "scenario-preview"

_UI_CORE_JS_TEMPLATE = r"""/* Screen logic shared by /scenario and /inspect. Nothing here touches the DOM. */
var UnitExpertUI = (function () {
  "use strict";

  var META_PROTOCOL_VERSION = "__META_PROTOCOL_VERSION__";
  var META_CLIENT_CAPABILITIES = "__META_CLIENT_CAPABILITIES__";
  var META_CLIENT_INFO = "__META_CLIENT_INFO__";
  var MODERN_VERSION = "__MODERN_VERSION__";
  var LEGACY_VERSION = "__LEGACY_VERSION__";
  var SCENARIO_HEADER = "__SCENARIO_HEADER__";
  var PREVIEW_SOURCE = "__PREVIEW_SOURCE__";
  var CLI_PREFIX = "python3 -m unit_expert_mcp.client";
  var CLIENT_INFO = { name: "scenario-ui", version: "1.0" };
  var BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function deepEqual(a, b) {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) {
      return a.length === b.length && a.every(function (item, index) { return deepEqual(item, b[index]); });
    }
    if (isObject(a) && isObject(b)) {
      var keys = Object.keys(a);
      return keys.length === Object.keys(b).length && keys.every(function (key) {
        return Object.prototype.hasOwnProperty.call(b, key) && deepEqual(a[key], b[key]);
      });
    }
    return false;
  }

  function getPath(source, path) {
    return String(path).split(".").reduce(function (value, key) {
      return value === undefined || value === null ? undefined : value[key];
    }, source);
  }

  /* Sets a dotted path in place and returns the target. */
  function setPath(target, path, value) {
    var keys = String(path).split(".");
    var node = target;
    keys.slice(0, -1).forEach(function (key) {
      if (!isObject(node[key])) node[key] = {};
      node = node[key];
    });
    node[keys[keys.length - 1]] = value;
    return target;
  }

  /* Deep merge of a partial config onto a copy of base (objects merge, the rest replaces). */
  function applyPatch(base, patch) {
    var result = clone(base) || {};
    Object.keys(patch || {}).forEach(function (key) {
      var value = patch[key];
      result[key] = isObject(value) && isObject(result[key]) ? applyPatch(result[key], value) : clone(value);
    });
    return result;
  }

  /* The part of config that differs from defaults (same rule as the server's compact_config). */
  function compactConfig(config, defaults) {
    function diff(value, defaultValue) {
      if (isObject(value) && isObject(defaultValue)) {
        var result = {};
        Object.keys(value).forEach(function (key) {
          var child = diff(value[key], defaultValue[key]);
          if (child !== undefined) result[key] = child;
        });
        return Object.keys(result).length ? result : undefined;
      }
      if (value === undefined || deepEqual(value, defaultValue)) return undefined;
      return value;
    }
    return diff(config, defaults) || {};
  }

  /* The rewrites the server's normalize_config applies, so a token made here
     has the same bytes as the server's. */
  function normalizeDerived(config) {
    var result = clone(config) || {};
    var era = result.serverEra || "both";
    if (era === "both" && result.rejectLegacy === true) era = "modern";
    result.serverEra = era;
    result.rejectLegacy = era === "modern";
    if (isObject(result.toolsList)) {
      var mode = result.toolsList.mode;
      result.toolsList.duplicateToolName =
        (mode === "normal" || mode === "too-many") && result.toolsList.duplicateToolName === true;
    }
    [["mcp", "identifier"], ["mcp", "serviceName"], ["initialize", "protocolVersion"]].forEach(function (pair) {
      var section = result[pair[0]];
      if (isObject(section) && typeof section[pair[1]] === "string") section[pair[1]] = section[pair[1]].trim();
    });
    if (Array.isArray(result.rawResponses)) {
      result.rawResponses = result.rawResponses.map(function (rule) {
        if (!isObject(rule)) return rule;
        return {
          enabled: rule.enabled === undefined ? true : rule.enabled,
          method: typeof rule.method === "string" ? rule.method.trim() : rule.method,
          status: rule.status === undefined ? 200 : rule.status,
          headers: rule.headers === undefined ? {} : rule.headers,
          contentType: rule.contentType === undefined ? "json" : rule.contentType,
          body: rule.body === undefined ? "" : rule.body
        };
      });
    }
    return result;
  }

  function bytesToBase64Url(bytes) {
    var out = "";
    for (var i = 0; i < bytes.length; i += 3) {
      var b0 = bytes[i];
      var b1 = i + 1 < bytes.length ? bytes[i + 1] : 0;
      var b2 = i + 2 < bytes.length ? bytes[i + 2] : 0;
      out += BASE64URL[b0 >> 2] + BASE64URL[((b0 & 3) << 4) | (b1 >> 4)];
      if (i + 1 < bytes.length) out += BASE64URL[((b1 & 15) << 2) | (b2 >> 6)];
      if (i + 2 < bytes.length) out += BASE64URL[b2 & 63];
    }
    return out;
  }

  function base64UrlToBytes(text) {
    var clean = text.replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
    if (!/^[A-Za-z0-9_-]*$/.test(clean) || clean.length % 4 === 1) throw new Error("invalid config token");
    var bytes = [];
    for (var i = 0; i < clean.length; i += 4) {
      var c0 = BASE64URL.indexOf(clean[i]);
      var c1 = BASE64URL.indexOf(clean[i + 1]);
      var c2 = i + 2 < clean.length ? BASE64URL.indexOf(clean[i + 2]) : -1;
      var c3 = i + 3 < clean.length ? BASE64URL.indexOf(clean[i + 3]) : -1;
      bytes.push((c0 << 2) | (c1 >> 4));
      if (c2 >= 0) bytes.push(((c1 & 15) << 4) | (c2 >> 2));
      if (c3 >= 0) bytes.push(((c2 & 3) << 6) | c3);
    }
    return new Uint8Array(bytes);
  }

  /* A number the way Python's json.dumps writes a float (5 -> "5.0"). */
  function pythonFloat(value) {
    var abs = Math.abs(value);
    if (Number.isInteger(value) && abs < 1e16) return value.toFixed(1);
    if (abs < 1e-4 || abs >= 1e16) {
      var parts = value.toExponential().split("e");
      var exponent = parseInt(parts[1], 10);
      var digits = String(Math.abs(exponent));
      return parts[0] + "e" + (exponent < 0 ? "-" : "+") + (digits.length < 2 ? "0" + digits : digits);
    }
    return String(value);
  }

  function floatPaths(schema) {
    var paths = {};
    (schema || []).forEach(function (option) {
      if (option.kind === "float") paths[option.path] = true;
    });
    return paths;
  }

  /* Compact JSON with the float options written like Python does. */
  function serialize(value, floats, path) {
    if (value === undefined) return "null";
    if (typeof value === "number" && floats[path] && isFinite(value)) return pythonFloat(value);
    if (Array.isArray(value)) {
      return "[" + value.map(function (item) { return serialize(item, floats, path + "[]"); }).join(",") + "]";
    }
    if (isObject(value)) {
      return "{" + Object.keys(value).filter(function (key) { return value[key] !== undefined; }).map(function (key) {
        return JSON.stringify(key) + ":" + serialize(value[key], floats, path ? path + "." + key : key);
      }).join(",") + "}";
    }
    return JSON.stringify(value);
  }

  /* cfg token of a config: base64url of the compact JSON, "" for the defaults. */
  function encodeToken(config, defaults, schema) {
    var compact = compactConfig(normalizeDerived(config), defaults);
    if (!Object.keys(compact).length) return "";
    return bytesToBase64Url(new TextEncoder().encode(serialize(compact, floatPaths(schema), "")));
  }

  /* The partial config inside a token. With defaults, the full config. */
  function decodeToken(token, defaults) {
    var text = String(token === undefined || token === null ? "" : token).trim();
    if (!text) throw new Error("empty config token");
    var payload;
    try {
      payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(base64UrlToBytes(text)));
    } catch (error) {
      throw new Error("invalid config token");
    }
    if (!isObject(payload)) return defaults ? clone(defaults) : {};
    return defaults ? normalizeDerived(applyPatch(defaults, payload)) : payload;
  }

  function truncate(text, limit) {
    return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
  }

  /* A config value as short display text. */
  function formatValue(value, option) {
    if (value === true) return "켬";
    if (value === false) return "끔";
    if (option && option.kind === "json") {
      return Array.isArray(value) ? (value.length ? "규칙 " + value.length + "개" : "비움") : truncate(JSON.stringify(value), 60);
    }
    if (Array.isArray(value)) {
      if (!value.length) return "비움";
      return truncate(value.map(function (item) {
        return typeof item === "string" ? item : JSON.stringify(item);
      }).join(", "), 60);
    }
    if (isObject(value)) return truncate(JSON.stringify(value), 60);
    if (value === undefined || value === null) return "없음";
    return truncate(String(value), 60);
  }

  function optionByPath(schema, path) {
    return (schema || []).filter(function (option) { return option.path === path; })[0] || null;
  }

  /* Whether an option has any effect under config: {effective, reason}.
     With schema, an option whose dependsOn option is itself without effect is
     without effect too. */
  function optionState(option, config, schema, depth) {
    var era = getPath(config, "serverEra") || "both";
    if (option.appliesTo === "modern" && era === "legacy") {
      return { effective: false, reason: "지원 스펙이 '2025 방식만'이라 적용되지 않습니다" };
    }
    if (option.appliesTo === "legacy" && era === "modern") {
      return { effective: false, reason: "지원 스펙이 '2026-07-28만'이라 적용되지 않습니다" };
    }
    if (option.strictOnly && getPath(config, "strict2026") === false) {
      return { effective: false, reason: "엄격 검증이 꺼져 있어 적용되지 않습니다" };
    }
    var condition = option.dependsOn;
    if (condition) {
      var value = getPath(config, condition.path);
      var parent = optionByPath(schema, condition.path);
      var parentName = parent ? "'" + parent.label + "'" : condition.path;
      if (Array.isArray(condition["in"])) {
        var allowed = condition["in"].some(function (item) { return deepEqual(item, value); });
        if (!allowed) {
          return {
            effective: false,
            reason: parentName + " 값이 " + condition["in"].map(function (item) {
              return formatValue(item, null);
            }).join(" 또는 ") + "일 때만 적용됩니다"
          };
        }
      }
      if (typeof condition.min === "number" && !(typeof value === "number" && value >= condition.min)) {
        return { effective: false, reason: parentName + " 값이 " + condition.min + " 이상일 때만 적용됩니다" };
      }
      if (parent && (depth || 0) < 8) {
        var parentState = optionState(parent, config, schema, (depth || 0) + 1);
        if (!parentState.effective) return parentState;
      }
    }
    return { effective: true, reason: "" };
  }

  function leafPaths(value, prefix, stopAt, out) {
    Object.keys(value).forEach(function (key) {
      var path = prefix ? prefix + "." + key : key;
      if (!stopAt[path] && isObject(value[key]) && Object.keys(value[key]).length) {
        leafPaths(value[key], path, stopAt, out);
      } else {
        out.push(path);
      }
    });
    return out;
  }

  /* The options that differ from the defaults, as chips:
     {path, label, value, effective, reason, hidden}. hidden marks a config
     field the form has no control for. */
  function diffSummary(config, defaults, schema) {
    var chips = [];
    var covered = {};
    (schema || []).forEach(function (option) {
      covered[option.path] = true;
      var value = getPath(config, option.path);
      if (value === undefined || deepEqual(value, getPath(defaults, option.path))) return;
      var state = optionState(option, config, schema);
      chips.push({
        path: option.path,
        label: option.label,
        value: formatValue(value, option),
        effective: state.effective,
        reason: state.reason,
        hidden: false
      });
    });
    leafPaths(compactConfig(config, defaults), "", covered, []).forEach(function (path) {
      /* rejectLegacy follows serverEra, which has its own chip. */
      if (covered[path] || path === "rejectLegacy") return;
      chips.push({
        path: path,
        label: path,
        value: formatValue(getPath(config, path), null),
        effective: true,
        reason: "",
        hidden: true
      });
    });
    return chips;
  }

  /* A partial config as {path: value}, one entry per schema option it sets. */
  function patchToOverrides(patch, schema) {
    var stopAt = {};
    (schema || []).forEach(function (option) { stopAt[option.path] = true; });
    var overrides = {};
    leafPaths(patch || {}, "", stopAt, []).forEach(function (path) {
      overrides[path] = clone(getPath(patch, path));
    });
    return overrides;
  }

  /* base with the {path: value} overrides written over it. Every field of
     base that has no override is kept as it is. */
  function mergeFormOverrides(baseConfig, overrides) {
    var result = clone(baseConfig) || {};
    Object.keys(overrides || {}).forEach(function (path) {
      if (overrides[path] !== undefined) setPath(result, path, clone(overrides[path]));
    });
    if (overrides && overrides.serverEra !== undefined && overrides.rejectLegacy === undefined) {
      result.rejectLegacy = result.serverEra === "modern";
    }
    return result;
  }

  /* Range problems the form can tell without asking the server: [{path, message}]. */
  function localErrors(config, schema) {
    var errors = [];
    (schema || []).forEach(function (option) {
      if (option.kind !== "int" && option.kind !== "float") return;
      var value = getPath(config, option.path);
      if (typeof value !== "number" || !isFinite(value)) {
        errors.push({ path: option.path, message: option.path + "는 숫자여야 합니다" });
      } else if (option.kind === "int" && !Number.isInteger(value)) {
        errors.push({ path: option.path, message: option.path + "는 정수여야 합니다" });
      } else if (
        (typeof option.min === "number" && value < option.min) ||
        (typeof option.max === "number" && value > option.max)
      ) {
        errors.push({
          path: option.path,
          message: option.path + "는 " + option.min + "~" + option.max + " 사이여야 합니다"
        });
      }
    });
    return errors;
  }

  function buildMcpUrl(origin, token) {
    return String(origin || "").replace(/\/+$/, "") + "/mcp" + (token ? "?cfg=" + encodeURIComponent(token) : "");
  }

  function scenarioHeaderLine(token) {
    return token ? SCENARIO_HEADER + ": cfg:" + token : "";
  }

  function modernMeta() {
    var meta = {};
    meta[META_PROTOCOL_VERSION] = MODERN_VERSION;
    meta[META_CLIENT_CAPABILITIES] = {};
    meta[META_CLIENT_INFO] = CLIENT_INFO;
    return meta;
  }

  /* One /mcp request: {label, headers, body}.
     kind: legacy-initialize | legacy-tools-list | modern-discover | modern-tools-list. */
  function buildRequest(kind) {
    if (kind === "legacy-initialize" || kind === "legacy-tools-list") {
      var legacyHeaders = { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" };
      if (kind === "legacy-initialize") {
        return {
          label: "POST /mcp initialize",
          headers: legacyHeaders,
          body: {
            jsonrpc: "2.0",
            id: 1,
            method: "initialize",
            params: { protocolVersion: LEGACY_VERSION, capabilities: {}, clientInfo: CLIENT_INFO }
          }
        };
      }
      return {
        label: "POST /mcp tools/list",
        headers: legacyHeaders,
        body: { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }
      };
    }
    if (kind === "modern-discover" || kind === "modern-tools-list") {
      var method = kind === "modern-discover" ? "server/discover" : "tools/list";
      return {
        label: "POST /mcp " + method,
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "MCP-Protocol-Version": MODERN_VERSION,
          "Mcp-Method": method
        },
        body: { jsonrpc: "2.0", id: kind === "modern-discover" ? 1 : 2, method: method, params: { _meta: modernMeta() } }
      };
    }
    throw new Error("unknown request kind '" + kind + "'");
  }

  function shellQuote(text) {
    return "'" + String(text).replace(/'/g, "'\\''") + "'";
  }

  /* curl command for one request kind of buildRequest. */
  function buildCurl(kind, url, extraHeaders) {
    var request = buildRequest(kind);
    var headers = Object.assign({}, request.headers, extraHeaders || {});
    var lines = ["curl -i -sS -X POST " + shellQuote(url)];
    Object.keys(headers).forEach(function (name) {
      lines.push("-H " + shellQuote(name + ": " + headers[name]));
    });
    lines.push("-d " + shellQuote(JSON.stringify(request.body)));
    return lines.join(" \\\n  ");
  }

  /* Test client command. command: "probe" | "run all" | "run <groups>".
     options: {expect, prefix}. probe takes no --expect. */
  function buildCliCommand(command, url, options) {
    var settings = options || {};
    var parts = [settings.prefix || CLI_PREFIX, command];
    if (String(command).split(" ")[0] === "run") parts.push("--expect " + (settings.expect || "server"));
    parts.push("--url " + shellQuote(url));
    return parts.join(" ");
  }

  /* Every word of query appears in the option's label, key, description or choices. */
  function matchOptionSearch(option, query) {
    var words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return true;
    var parts = [option.label, option.path, option.description];
    (option.choices || []).concat(option.suggestions || []).forEach(function (choice) {
      parts.push(String(choice.value), choice.label || "", choice.description || "");
    });
    var haystack = parts.join("\n").toLowerCase();
    return words.every(function (word) { return haystack.indexOf(word) >= 0; });
  }

  /* An injected label that only made the response wait (…delaySeconds=N). */
  function isDelayLabel(label) {
    return /(^|\.)delaySeconds=/.test(String(label));
  }

  /* How the request was made: "2026", "2025" (legacy) or "" when it cannot be
     told (no JSON-RPC request was read: OPTIONS, GET, DELETE, a body that did
     not parse) and nothing marks it as a 2026 request. */
  function requestMode(exchange) {
    if (exchange.era === "2026") return "2026";
    if (exchange.httpMethod && exchange.httpMethod !== "POST") return "";
    if (typeof exchange.method !== "string" || !exchange.method) return "";
    return exchange.era || "";
  }

  /* What kind of answer an /inspect/log entry is:
     {kind, label, detail, code, codeName, delays}. kind: closed | raw |
     injected | rejected | ok. delays lists the injected waits. A response
     that was only delayed keeps the kind it would have had without the wait. */
  function classifyExchange(exchange, errorCodes) {
    var injected = exchange.injected || [];
    var delays = injected.filter(isDelayLabel);
    var shaping = injected.filter(function (label) { return !isDelayLabel(label); });
    var body = exchange.responseBody;
    var error = isObject(body) && isObject(body.error) ? body.error : null;
    var code = error && typeof error.code === "number" ? error.code : null;
    var known = code === null ? null : (errorCodes || []).filter(function (entry) { return entry.code === code; })[0];
    var codeName = known ? known.name : null;

    function result(kind, label, detail) {
      return { kind: kind, label: label, detail: detail, code: code, codeName: codeName, delays: delays };
    }

    if (!exchange.status) return result("closed", "연결 종료", injected.join(", "));
    if (exchange.rawResponse) return result("raw", "직접 지정", "직접 지정한 응답(rawResponses)을 그대로 줌");
    if (shaping.length) return result("injected", "넣은 에러", injected.join(", "));
    if (code !== null) {
      return result("rejected", "서버가 거절", code + (codeName ? " " + codeName : "") + (error.message ? ": " + error.message : ""));
    }
    if (exchange.status >= 400) {
      var text = isObject(body) && typeof body.error === "string" ? body.error : "";
      return result("rejected", "서버가 거절", "HTTP " + exchange.status + (text ? ": " + text : ""));
    }
    return result("ok", "정상", delays.length ? "늦게 답함: " + delays.join(", ") : "");
  }

  function statusClass(status) {
    return status ? Math.floor(status / 100) + "xx" : "0";
  }

  /* The tool a tools/call request names, or "". */
  function toolName(exchange) {
    var body = exchange.requestBody;
    var params = isObject(body) && isObject(body.params) ? body.params : null;
    return exchange.method === "tools/call" && params && typeof params.name === "string" ? params.name : "";
  }

  /* filters: cfg ("" = requests without a token), method, era, configSource,
     source, hidePreview (the /scenario preview), hideScreen (any request that
     names a source), hideOptions (HTTP OPTIONS), status ("2xx" | "4xx" | "5xx"
     | "0"), kind (one or a list of classifyExchange kinds), errorsOnly, text,
     errorCodes.
     filterBlockers: the names of the filters that keep one exchange out of
     the list ([] when it is shown). text also looks at what the list shows
     for the exchange: the result label and the error code name. */
  function filterBlockers(exchange, filters) {
    var f = filters || {};
    var blockers = [];
    var text = String(f.text || "").trim().toLowerCase();
    var kinds = f.kind ? (Array.isArray(f.kind) ? f.kind : [f.kind]) : null;
    if (f.cfg !== undefined && f.cfg !== null && (exchange.cfg || "") !== f.cfg) blockers.push("cfg");
    if (f.method && exchange.method !== f.method) blockers.push("method");
    if (f.era && requestMode(exchange) !== f.era) blockers.push("era");
    if (f.configSource && exchange.configSource !== f.configSource) blockers.push("configSource");
    if (f.source && exchange.source !== f.source) blockers.push("source");
    if (f.hidePreview && exchange.source === PREVIEW_SOURCE) blockers.push("hidePreview");
    if (f.hideScreen && exchange.source) blockers.push("hideScreen");
    if (f.hideOptions && exchange.httpMethod === "OPTIONS") blockers.push("hideOptions");
    if (f.status && statusClass(exchange.status) !== f.status) blockers.push("status");
    var info = kinds || f.errorsOnly || text ? classifyExchange(exchange, f.errorCodes) : null;
    if (kinds && kinds.indexOf(info.kind) < 0) blockers.push("kind");
    if (f.errorsOnly && info.kind === "ok") blockers.push("errorsOnly");
    if (text) {
      var haystack = JSON.stringify([
        exchange.method, exchange.httpMethod, exchange.path, exchange.query,
        exchange.requestBody, exchange.responseBody, exchange.injected,
        exchange.requestHeaders, exchange.responseHeaders, exchange.cfg,
        info.label, info.code === null ? "" : info.code + " " + (info.codeName || "")
      ]).toLowerCase();
      if (haystack.indexOf(text) < 0) blockers.push("text");
    }
    return blockers;
  }

  function filterExchanges(list, filters) {
    return (list || []).filter(function (exchange) {
      return filterBlockers(exchange, filters).length === 0;
    });
  }

  /* A copy of headers with credentials hidden. A Bearer/Basic scheme and the
     last four characters of a long value stay readable. */
  function maskSecretHeaders(headers) {
    var masked = {};
    Object.keys(headers || {}).forEach(function (name) {
      var lowered = name.toLowerCase();
      var value = headers[name];
      var secret = lowered === "authorization" || lowered === "proxy-authorization" ||
        lowered === "cookie" || lowered === "set-cookie" || /token|secret|password|api-key/.test(lowered);
      if (!secret || typeof value !== "string") {
        masked[name] = value;
        return;
      }
      var match = /^(\S+)\s+(.+)$/.exec(value);
      var scheme = match && (lowered === "authorization" || lowered === "proxy-authorization") ? match[1] + " " : "";
      var credential = scheme ? match[2] : value;
      masked[name] = scheme + "***" + (credential.length >= 12 ? credential.slice(-4) : "");
    });
    return masked;
  }

  function pad(value, width) {
    var text = String(value);
    while (text.length < width) text = "0" + text;
    return text;
  }

  /* An ISO timestamp as local HH:MM:SS.mmm. "" when it does not parse. */
  function formatLocalTime(iso) {
    var date = new Date(iso);
    if (!iso || isNaN(date.getTime())) return "";
    return pad(date.getHours(), 2) + ":" + pad(date.getMinutes(), 2) + ":" + pad(date.getSeconds(), 2) +
      "." + pad(date.getMilliseconds(), 3);
  }

  /* formatLocalTime, with the local date (MM-DD) in front when it is not the
     local date of now (a Date value or timestamp; default: the current time). */
  function formatLocalStamp(iso, now) {
    var time = formatLocalTime(iso);
    if (!time) return "";
    var date = new Date(iso);
    var today = now === undefined || now === null ? new Date() : new Date(now);
    if (
      date.getFullYear() === today.getFullYear() &&
      date.getMonth() === today.getMonth() &&
      date.getDate() === today.getDate()
    ) return time;
    var prefix = date.getFullYear() === today.getFullYear() ? "" : date.getFullYear() + "-";
    return prefix + pad(date.getMonth() + 1, 2) + "-" + pad(date.getDate(), 2) + " " + time;
  }

  /* What a cfg token changes, as one line: {ok, chips, text}. */
  function tokenSummary(token, defaults, schema) {
    try {
      var chips = diffSummary(decodeToken(token, defaults), defaults, schema);
      return {
        ok: true,
        chips: chips,
        text: chips.length
          ? chips.map(function (chip) { return chip.label + " = " + chip.value; }).join(", ")
          : "기본 설정과 같음"
      };
    } catch (error) {
      return { ok: false, chips: [], text: "읽을 수 없는 설정 코드" };
    }
  }

  /* The cfg token inside pasted text: a bare token, cfg:<token>, cfg=<token>,
     a URL whose query carries cfg, or the header line
     "X-MCP-Test-Scenario: cfg:<token>". "" when there is none. */
  function tokenFromText(text) {
    var value = String(text === undefined || text === null ? "" : text).trim();
    if (!value) return "";
    var headerPrefix = SCENARIO_HEADER.toLowerCase() + ":";
    if (value.toLowerCase().indexOf(headerPrefix) === 0) value = value.slice(headerPrefix.length).trim();
    var match = /(?:^|[?&#])cfg=([^&#\s]*)/.exec(value);
    if (match) {
      try { return decodeURIComponent(match[1]); } catch (error) { return match[1]; }
    }
    if (/^cfg:/i.test(value)) return value.slice(4).trim();
    return /[?&=\/\s]/.test(value.replace(/=+$/, "")) ? "" : value;
  }

  var FILTER_KINDS = ["ok", "injected", "rejected", "raw", "closed"];
  var FILTER_STATUSES = ["2xx", "4xx", "5xx", "0"];
  var FILTER_ERAS = ["2025", "2026"];

  /* The /inspect filters when nothing is chosen. */
  function defaultFilters() {
    return {
      text: "", kind: "", status: "", era: "", method: "", cfg: "", plain: false,
      hideScreen: true, hideOptions: true, errorsOnly: false
    };
  }

  /* /inspect filters from the query string of the page URL. Unknown values are dropped. */
  function filtersFromQuery(search) {
    var params = new URLSearchParams(String(search || "").replace(/^\?/, ""));
    var filters = defaultFilters();
    function pick(name, allowed) {
      var value = params.get(name) || "";
      return !allowed || allowed.indexOf(value) >= 0 ? value : "";
    }
    filters.text = pick("q");
    filters.kind = pick("kind", FILTER_KINDS);
    filters.status = pick("status", FILTER_STATUSES);
    filters.era = pick("era", FILTER_ERAS);
    filters.method = pick("method");
    filters.cfg = pick("cfg").trim();
    /* plain: only the requests to the address without a config. */
    filters.plain = !filters.cfg && params.get("plain") === "1";
    filters.hideScreen = params.get("screen") !== "1";
    filters.hideOptions = params.get("options") !== "1";
    filters.errorsOnly = params.get("errors") === "1";
    return filters;
  }

  /* The query string ("" or "?…") that filtersFromQuery reads back as filters. */
  function filtersToQuery(filters) {
    var f = filters || {};
    var parts = [];
    function add(name, value) {
      if (value) parts.push(name + "=" + encodeURIComponent(value));
    }
    add("cfg", f.cfg);
    if (f.plain && !f.cfg) parts.push("plain=1");
    add("q", f.text);
    add("kind", f.kind);
    add("status", f.status);
    add("era", f.era);
    add("method", f.method);
    if (f.hideScreen === false) parts.push("screen=1");
    if (f.hideOptions === false) parts.push("options=1");
    if (f.errorsOnly) parts.push("errors=1");
    return parts.length ? "?" + parts.join("&") : "";
  }

  /* An Mcp-Name / Mcp-Param-* header value: {value, encoded, valid}. A
     =?base64?…?= value is decoded as UTF-8; valid is false when that fails. */
  function decodeHeaderValue(raw) {
    var text = String(raw);
    var prefix = "=?base64?";
    var suffix = "?=";
    if (text.length < prefix.length + suffix.length || text.indexOf(prefix) !== 0 || text.slice(-suffix.length) !== suffix) {
      return { value: text, encoded: false, valid: true };
    }
    var payload = text.slice(prefix.length, -suffix.length);
    try {
      if (!/^[A-Za-z0-9+\/]*={0,2}$/.test(payload) || payload.length % 4 !== 0) throw new Error("not base64");
      var bytes = base64UrlToBytes(payload);
      return { value: new TextDecoder("utf-8", { fatal: true }).decode(bytes), encoded: true, valid: true };
    } catch (error) {
      return { value: text, encoded: true, valid: false };
    }
  }

  var COMPARE_LABELS = {
    "match": "일치",
    "differ": "다름",
    "header-missing": "헤더 없음",
    "body-missing": "바디에 값 없음",
    "invalid": "디코드 실패",
    "none": "둘 다 없음"
  };

  /* The routing headers of a 2026 request next to the body values they
     mirror: [{header, headerValue, decodedValue, bodyLabel, bodyValue, state,
     stateLabel}]. [] for a legacy request. An Mcp-Param-* header is paired
     with the argument of the same name (case-insensitive).
     headerParams ({tool name: [{header, argument}]}, optional) lists the
     arguments a tool mirrors into Mcp-Param-* headers: an argument that has a
     value but no header gets a "header-missing" row. The same row is made for
     a header the server names as missing in its refusal. */
  function headerBodyRows(exchange, headerParams) {
    if (exchange.era !== "2026") return [];
    var headers = {};
    Object.keys(exchange.requestHeaders || {}).forEach(function (name) {
      headers[name.toLowerCase()] = { name: name, value: exchange.requestHeaders[name] };
    });
    var body = isObject(exchange.requestBody) ? exchange.requestBody : {};
    var params = isObject(body.params) ? body.params : {};
    var meta = isObject(params._meta) ? params._meta : {};
    var args = isObject(params.arguments) ? params.arguments : {};
    var rows = [];

    function add(headerName, bodyLabel, bodyValue) {
      var entry = headers[headerName.toLowerCase()];
      var decoded = entry ? decodeHeaderValue(entry.value) : null;
      var hasBody = bodyValue !== undefined && bodyValue !== null;
      var bodyText = hasBody ? (typeof bodyValue === "string" ? bodyValue : JSON.stringify(bodyValue)) : null;
      var state;
      if (!entry && !hasBody) state = "none";
      else if (!entry) state = "header-missing";
      else if (!decoded.valid) state = "invalid";
      else if (!hasBody) state = "body-missing";
      else state = decoded.value === bodyText ? "match" : "differ";
      rows.push({
        header: entry ? entry.name : headerName,
        headerValue: entry ? String(entry.value) : null,
        decodedValue: decoded && decoded.encoded && decoded.valid ? decoded.value : null,
        bodyLabel: bodyLabel,
        bodyValue: bodyText,
        state: state,
        stateLabel: COMPARE_LABELS[state]
      });
    }

    add("MCP-Protocol-Version", "_meta protocolVersion", meta[META_PROTOCOL_VERSION]);
    add("Mcp-Method", "method", body.method);
    if (headers["mcp-name"] || params.name !== undefined || params.uri !== undefined) {
      var useUri = params.name === undefined && params.uri !== undefined;
      add("Mcp-Name", useUri ? "params.uri" : "params.name", useUri ? params.uri : params.name);
    }
    Object.keys(headers).sort().forEach(function (lowered) {
      if (lowered.indexOf("mcp-param-") !== 0) return;
      var suffix = lowered.slice("mcp-param-".length);
      var key = Object.keys(args).filter(function (name) { return name.toLowerCase() === suffix; })[0];
      add(headers[lowered].name, "params.arguments." + (key || suffix), key === undefined ? undefined : args[key]);
    });

    function addMissing(headerSuffix, argument) {
      if (headers["mcp-param-" + String(headerSuffix).toLowerCase()]) return;
      var header = "Mcp-Param-" + headerSuffix;
      if (rows.some(function (row) { return row.header.toLowerCase() === header.toLowerCase(); })) return;
      var value = getPath(args, argument);
      if (value === undefined || value === null) return;
      add(header, "params.arguments." + argument, value);
    }
    if (body.method === "tools/call") {
      var declared = isObject(headerParams) && typeof params.name === "string" ? headerParams[params.name] : null;
      (Array.isArray(declared) ? declared : []).forEach(function (entry) {
        addMissing(entry.header, entry.argument);
      });
      var response = exchange.responseBody;
      var message = isObject(response) && isObject(response.error) ? String(response.error.message || "") : "";
      var missing = /Missing Mcp-Param-(\S+) header for argument '([^']+)'/.exec(message);
      if (missing) addMissing(missing[1], missing[2]);
    }
    return rows;
  }

  /* A config error of the server (English, starts with the config path) for
     display: {path, option, text}. path is the config path the message names
     ("" when none), option the schema option it belongs to (or null) and text
     "설정 오류: <label (path)> — <message>". */
  function describeConfigError(message, schema) {
    var original = String(message === undefined || message === null ? "" : message).trim();
    var match = /^([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+|\[\d+\])*)(?=\s|$)/.exec(original);
    var named = match ? match[1] : "";
    var option = null;
    (schema || []).forEach(function (candidate) {
      var path = candidate.path;
      var next = named.charAt(path.length);
      if (named.indexOf(path) === 0 && (next === "" || next === "." || next === "[")) {
        if (!option || path.length > option.path.length) option = candidate;
      }
    });
    if (!option) return { path: "", option: null, text: "설정 오류: " + original };
    return {
      path: named,
      option: option,
      text: "설정 오류: " + option.label + " (" + named + ") — " + original
    };
  }

  /* curl command that sends a recorded exchange again: {command, placeholders}.
     Unless reveal is true, a credential header carries a placeholder instead
     of its value and its name is listed in placeholders. */
  function buildExchangeCurl(exchange, origin, reveal) {
    var headers = exchange.requestHeaders || {};
    var masked = maskSecretHeaders(headers);
    var placeholders = [];
    var method = /^[A-Z]+$/.test(exchange.httpMethod || "") ? exchange.httpMethod : "POST";
    var url = String(origin || "").replace(/\/+$/, "") + (exchange.path || "/mcp") +
      (exchange.query ? "?" + exchange.query : "");
    var lines = ["curl -i -sS -X " + method + " " + shellQuote(url)];
    Object.keys(headers).forEach(function (name) {
      var value = headers[name];
      if (!reveal && masked[name] !== value) {
        placeholders.push(name);
        value = "<" + name + " 값>";
      }
      lines.push("-H " + shellQuote(name + ": " + value));
    });
    var body = exchange.requestBody;
    if (body !== null && body !== undefined) {
      lines.push("-d " + shellQuote(typeof body === "string" ? body : JSON.stringify(body)));
    }
    return { command: lines.join(" \\\n  "), placeholders: placeholders };
  }

  /* ---- pasted text: requests, configs ---------------------------------- */

  /* Index of the first character JSON.parse cannot take (text.length when
     the text ends too early), or -1 for valid JSON. The engines word their
     errors differently and often leave the position out, so it is found here. */
  function jsonErrorPosition(text) {
    var source = String(text);
    var index = 0;
    function fail() { throw { at: index }; }
    function space() { while (/[ \t\n\r]/.test(source[index] || "")) index += 1; }
    function string() {
      index += 1;
      while (index < source.length && source[index] !== '"') {
        if (source[index] === "\n") fail();
        if (source[index] === "\\") {
          index += 1;
          if (source[index] === "u") {
            if (!/^[0-9a-fA-F]{4}$/.test(source.slice(index + 1, index + 5))) fail();
            index += 4;
          } else if ('"\\/bfnrt'.indexOf(source[index] || "?") < 0) fail();
        }
        index += 1;
      }
      if (index >= source.length) fail();
      index += 1;
    }
    function value() {
      space();
      var ch = source[index];
      if (ch === "{" || ch === "[") {
        var close = ch === "{" ? "}" : "]";
        index += 1;
        space();
        if (source[index] === close) { index += 1; return; }
        for (;;) {
          space();
          if (close === "}") {
            if (source[index] !== '"') fail();
            string();
            space();
            if (source[index] !== ":") fail();
            index += 1;
          }
          value();
          space();
          if (source[index] === close) { index += 1; return; }
          if (source[index] !== ",") fail();
          index += 1;
        }
      }
      if (ch === '"') { string(); return; }
      var literal = /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(source.slice(index, index + 400));
      if (!literal) fail();
      index += literal[0].length;
    }
    try {
      value();
      space();
      if (index < source.length) fail();
      return -1;
    } catch (error) {
      if (error && typeof error.at === "number") return Math.min(error.at, source.length);
      throw error;
    }
  }

  /* One line that says where JSON.parse gave up: "2번째 줄 5번째 글자 근처". */
  function jsonErrorLine(text, error) {
    var message = error && error.message ? String(error.message) : "";
    var source = String(text);
    var position = jsonErrorPosition(source);
    var where = "";
    if (position >= 0) {
      var before = source.slice(0, position).split("\n");
      where = before.length + "번째 줄 " + (before[before.length - 1].length + 1) + "번째 글자 근처";
      if (position >= source.length) where += "(끝이 잘렸습니다)";
    }
    return "JSON을 읽지 못했습니다" + (where ? ": " + where : "") + (message ? " — " + message.replace(/\s+/g, " ") : "");
  }

  /* Words of a shell command line: quotes, backslashes and line continuations. */
  function shellWords(text) {
    var words = [];
    var current = "";
    var has = false;
    var source = String(text);
    var index = 0;
    while (index < source.length) {
      var ch = source[index];
      if (ch === "\\" && (source[index + 1] === "\n" || source.slice(index + 1, index + 3) === "\r\n")) {
        index += source[index + 1] === "\n" ? 2 : 3;
      } else if (ch === "'") {
        var close = source.indexOf("'", index + 1);
        if (close < 0) throw new Error("닫히지 않은 작은따옴표(')가 있습니다");
        current += source.slice(index + 1, close);
        has = true;
        index = close + 1;
      } else if (ch === '"') {
        index += 1;
        while (index < source.length && source[index] !== '"') {
          if (source[index] === "\\" && '"\\$`'.indexOf(source[index + 1]) >= 0) index += 1;
          current += source[index];
          index += 1;
        }
        if (index >= source.length) throw new Error("닫히지 않은 큰따옴표(\")가 있습니다");
        has = true;
        index += 1;
      } else if (ch === "\\") {
        current += source[index + 1] === undefined ? "" : source[index + 1];
        has = true;
        index += 2;
      } else if (/\s/.test(ch)) {
        if (has) words.push(current);
        current = "";
        has = false;
        index += 1;
      } else {
        current += ch;
        has = true;
        index += 1;
      }
    }
    if (has) words.push(current);
    return words;
  }

  var CURL_DATA_OPTIONS = ["-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json"];
  var CURL_VALUE_OPTIONS = ["-o", "--output", "-u", "--user", "-A", "--user-agent", "-m", "--max-time",
    "--connect-timeout", "-w", "--write-out", "-b", "--cookie", "-e", "--referer", "--retry"];

  /* A curl command line as {url, httpMethod, headers, body}. Throws an Error
     with a Korean message when it is not a curl command or has no URL. */
  function parseCurl(text) {
    var words = shellWords(text);
    if (!words.length || words[0] !== "curl") throw new Error("curl로 시작하는 명령이 아닙니다");
    var result = { url: "", httpMethod: "", headers: {}, body: null };
    for (var index = 1; index < words.length; index += 1) {
      var word = words[index];
      var inline = /^(--[a-z-]+)=([\s\S]*)$/.exec(word);
      var name = inline ? inline[1] : word;
      var take = function () {
        if (inline) return inline[2];
        index += 1;
        if (index >= words.length) throw new Error(name + " 뒤에 값이 없습니다");
        return words[index];
      };
      if (name === "-X" || name === "--request") {
        result.httpMethod = take().toUpperCase();
      } else if (name === "-H" || name === "--header") {
        var line = take();
        var colon = line.indexOf(":");
        if (colon > 0) result.headers[line.slice(0, colon).trim()] = line.slice(colon + 1).trim();
      } else if (CURL_DATA_OPTIONS.indexOf(name) >= 0) {
        result.body = take();
        if (name === "--json") {
          if (!headerValue(result.headers, "Content-Type")) result.headers["Content-Type"] = "application/json";
          if (!headerValue(result.headers, "Accept")) result.headers["Accept"] = "application/json";
        }
      } else if (name === "--url") {
        result.url = take();
      } else if (CURL_VALUE_OPTIONS.indexOf(name) >= 0) {
        take();
      } else if (word[0] !== "-" && !result.url) {
        result.url = word;
      }
    }
    if (!result.url) throw new Error("curl 명령에서 주소를 찾지 못했습니다");
    if (!result.httpMethod) result.httpMethod = result.body === null ? "GET" : "POST";
    return result;
  }

  function headerValue(headers, name) {
    var lowered = String(name).toLowerCase();
    var key = Object.keys(headers || {}).filter(function (item) { return item.toLowerCase() === lowered; })[0];
    return key === undefined ? "" : String(headers[key]);
  }

  /* headers with every entry of extra written over the same name (any case). */
  function mergeHeaders(headers, extra) {
    var result = Object.assign({}, headers);
    Object.keys(extra || {}).forEach(function (name) {
      Object.keys(result).forEach(function (key) {
        if (key.toLowerCase() === name.toLowerCase()) delete result[key];
      });
      result[name] = extra[name];
    });
    return result;
  }

  var REQUEST_FIELDS = ["url", "mode", "method", "tool", "args", "arguments", "params", "id", "headers", "omit", "body"];
  /* What "omit" may list, named like the client's --omit-* flags. */
  var REQUEST_OMIT = ["meta-version", "capabilities", "meta", "version-header", "method-header", "name-header"];
  var MODE_ALIASES = {
    "2026": "2026", "2026-07-28": "2026", "modern": "2026",
    "2025": "2025", "legacy": "2025"
  };
  var MODERN_ONLY_METHODS = ["server/discover", "subscriptions/listen"];
  var LEGACY_ONLY_METHODS = ["initialize", "notifications/initialized"];

  /* "2026" or "2025" for a JSON-RPC body (and the headers sent with it). */
  function guessRequestMode(body, headers) {
    var params = isObject(body) && isObject(body.params) ? body.params : {};
    var meta = isObject(params._meta) ? params._meta : {};
    if (meta[META_PROTOCOL_VERSION] !== undefined || meta[META_CLIENT_INFO] !== undefined ||
        meta[META_CLIENT_CAPABILITIES] !== undefined) return "2026";
    if (headerValue(headers, "Mcp-Method") || headerValue(headers, "MCP-Protocol-Version") === MODERN_VERSION) return "2026";
    var method = isObject(body) ? body.method : "";
    if (LEGACY_ONLY_METHODS.indexOf(method) >= 0) return "2025";
    if (MODERN_ONLY_METHODS.indexOf(method) >= 0) return "2026";
    return "2025";
  }

  function fieldsOfBody(body) {
    var params = isObject(body) && isObject(body.params) ? body.params : {};
    return {
      method: isObject(body) && typeof body.method === "string" ? body.method : "",
      tool: typeof params.name === "string" ? params.name : "",
      args: isObject(params.arguments) ? clone(params.arguments) : null
    };
  }

  /* What a pasted text asks to send: {ok, kind, request, error}.
     kind "jsonrpc": a JSON-RPC request body. kind "bundle": the request
     bundle of requestFormatText(). kind "curl": a curl command line.
     request: {url, mode, method, tool, args, params, id, headers,
     exactHeaders, omit, body, bodyText}. url "" means "keep the address".
     exactHeaders: headers is the whole header list (curl), not an addition.
     body / bodyText: the body to send as it is (object / text of curl). */
  function parseRequestPaste(text) {
    var source = String(text === undefined || text === null ? "" : text).trim();
    function fail(message) { return { ok: false, kind: "", request: null, error: message }; }
    function done(kind, request) {
      return {
        ok: true, kind: kind, error: "",
        request: Object.assign({
          url: "", mode: "2026", method: "", tool: "", args: null, params: null, id: undefined,
          headers: null, exactHeaders: false, omit: [], body: undefined, bodyText: undefined
        }, request)
      };
    }
    if (!source) return fail("붙여 넣은 내용이 없습니다");

    if (/^curl(\s|$)/.test(source)) {
      var curl;
      try { curl = parseCurl(source); } catch (error) { return fail("curl 명령을 읽지 못했습니다: " + error.message); }
      var curlBody;
      try { curlBody = curl.body === null ? undefined : JSON.parse(curl.body); } catch (error) { curlBody = undefined; }
      return done("curl", Object.assign(fieldsOfBody(curlBody), {
        url: curl.url,
        mode: guessRequestMode(curlBody, curl.headers),
        id: isObject(curlBody) ? curlBody.id : undefined,
        headers: curl.headers,
        exactHeaders: true,
        bodyText: curl.body === null ? "" : curl.body
      }));
    }

    var value;
    try { value = JSON.parse(source); } catch (error) { return fail(jsonErrorLine(source, error)); }
    if (!isObject(value)) return fail("JSON 객체({ … })여야 합니다");

    if (value.jsonrpc !== undefined) {
      if (typeof value.method !== "string" || !value.method) return fail("JSON-RPC 요청에 method가 없습니다");
      return done("jsonrpc", Object.assign(fieldsOfBody(value), {
        mode: guessRequestMode(value, null),
        id: value.id,
        body: value
      }));
    }

    var unknown = Object.keys(value).filter(function (key) { return REQUEST_FIELDS.indexOf(key) < 0; });
    if (unknown.length) {
      return fail("모르는 필드: " + unknown.join(", ") + ". 쓸 수 있는 필드: " +
        REQUEST_FIELDS.filter(function (key) { return key !== "arguments"; }).join(", "));
    }
    var body = value.body;
    var fromBody = fieldsOfBody(body);
    var method = value.method === undefined ? fromBody.method : value.method;
    if (typeof method !== "string" || !method) return fail("method(문자열)나 body가 필요합니다");
    var mode = value.mode === undefined ? (body === undefined ? "2026" : guessRequestMode(body, value.headers)) : MODE_ALIASES[String(value.mode)];
    if (!mode) return fail('mode는 "2026" 또는 "2025"여야 합니다');
    if (value.url !== undefined && typeof value.url !== "string") return fail("url은 문자열이어야 합니다");
    if (value.tool !== undefined && typeof value.tool !== "string") return fail("tool은 문자열이어야 합니다");
    var args = value.args !== undefined ? value.args : value.arguments;
    if (args !== undefined && !isObject(args)) return fail("args는 JSON 객체여야 합니다");
    if (value.params !== undefined && !isObject(value.params)) return fail("params는 JSON 객체여야 합니다");
    if (value.headers !== undefined) {
      var badHeader = !isObject(value.headers) || Object.keys(value.headers).some(function (name) {
        return ["string", "number", "boolean"].indexOf(typeof value.headers[name]) < 0;
      });
      if (badHeader) return fail('headers는 {"이름": "값"} 객체여야 합니다');
    }
    var omit = value.omit === undefined ? [] : value.omit;
    if (!Array.isArray(omit) || omit.some(function (name) { return REQUEST_OMIT.indexOf(name) < 0; })) {
      return fail("omit에는 다음만 넣을 수 있습니다: " + REQUEST_OMIT.join(", "));
    }
    var headers = null;
    if (value.headers !== undefined) {
      headers = {};
      Object.keys(value.headers).forEach(function (name) { headers[name] = String(value.headers[name]); });
    }
    return done("bundle", {
      url: value.url || "",
      mode: mode,
      method: method,
      tool: value.tool !== undefined ? value.tool : fromBody.tool,
      args: args !== undefined ? clone(args) : fromBody.args,
      params: value.params === undefined ? null : clone(value.params),
      id: value.id !== undefined ? value.id : (isObject(body) ? body.id : undefined),
      headers: headers,
      omit: omit.slice(),
      body: body === undefined ? undefined : clone(body)
    });
  }

  /* An argument value the way a client mirrors it into an Mcp-Param-* header. */
  function paramHeaderValue(value) {
    var text = typeof value === "string" ? value : (typeof value === "object" ? JSON.stringify(value) : String(value));
    if (/^[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?$/.test(text) && text.indexOf("=?base64?") !== 0) return text;
    var bytes = new TextEncoder().encode(text);
    var encoded = bytesToBase64Url(bytes).replace(/-/g, "+").replace(/_/g, "/");
    while (encoded.length % 4) encoded += "=";
    return "=?base64?" + encoded + "?=";
  }

  /* The HTTP request a parsed request stands for: {headers, body, bodyText}.
     options: {clientName, headerParams ({tool: [{header, argument}]})}. */
  function buildRequestDraft(request, options) {
    var settings = options || {};
    var modern = request.mode !== "2025";
    var omit = request.omit || [];
    var method = request.method;
    var body;
    var bodyText;
    if (request.bodyText !== undefined) {
      bodyText = request.bodyText;
      try { body = JSON.parse(bodyText); } catch (error) { body = undefined; }
    } else if (request.body !== undefined) {
      body = clone(request.body);
      bodyText = typeof body === "string" ? body : JSON.stringify(body, null, 2);
    } else {
      var params = clone(request.params) || {};
      if (request.tool) params.name = request.tool;
      if (request.args) params.arguments = clone(request.args);
      else if (method === "tools/call" && params.arguments === undefined) params.arguments = {};
      if (modern) {
        if (method === "subscriptions/listen" && params.notifications === undefined) {
          params.notifications = { toolsListChanged: true };
        }
        if (omit.indexOf("meta") < 0) {
          var meta = modernMeta();
          if (settings.clientName) meta[META_CLIENT_INFO] = { name: settings.clientName, version: "1.0" };
          if (omit.indexOf("meta-version") >= 0) delete meta[META_PROTOCOL_VERSION];
          if (omit.indexOf("capabilities") >= 0) delete meta[META_CLIENT_CAPABILITIES];
          params._meta = Object.assign(meta, isObject(params._meta) ? params._meta : {});
        }
      } else if (method === "initialize") {
        params = Object.assign({
          protocolVersion: LEGACY_VERSION,
          capabilities: {},
          clientInfo: { name: settings.clientName || CLIENT_INFO.name, version: "1.0" }
        }, params);
      }
      body = { jsonrpc: "2.0", id: request.id === undefined ? 1 : request.id, method: method, params: params };
      if (!modern && method.indexOf("notifications/") === 0) delete body.id;
      bodyText = JSON.stringify(body, null, 2);
    }

    var headers;
    if (request.exactHeaders) {
      headers = Object.assign({}, request.headers);
    } else {
      var bodyParams = isObject(body) && isObject(body.params) ? body.params : {};
      if (modern) {
        headers = {
          "Content-Type": "application/json",
          "Accept": method === "subscriptions/listen" ? "text/event-stream" : "application/json"
        };
        if (omit.indexOf("version-header") < 0) headers["MCP-Protocol-Version"] = MODERN_VERSION;
        if (omit.indexOf("method-header") < 0) headers["Mcp-Method"] = method;
        if (method === "tools/call" && typeof bodyParams.name === "string" && omit.indexOf("name-header") < 0) {
          headers["Mcp-Name"] = paramHeaderValue(bodyParams.name);
        }
        var declared = isObject(settings.headerParams) ? settings.headerParams[bodyParams.name] : null;
        if (method === "tools/call" && Array.isArray(declared) && isObject(bodyParams.arguments)) {
          declared.forEach(function (entry) {
            var value = getPath(bodyParams.arguments, entry.argument);
            if (value !== undefined && value !== null) headers["Mcp-Param-" + entry.header] = paramHeaderValue(value);
          });
        }
      } else {
        headers = { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" };
      }
      headers = mergeHeaders(headers, request.headers);
    }
    return { headers: headers, body: body, bodyText: bodyText };
  }

  /* The request bundle of a form state, to copy: form {url, mode, method,
     tool, args, id} plus what the edited headers and body differ in from the
     draft the form alone would make. */
  function requestBundle(form, actualHeaders, actualBodyText, options) {
    var bundle = {};
    if (form.url) bundle.url = form.url;
    bundle.mode = form.mode === "2025" ? "2025" : "2026";
    bundle.method = form.method;
    if (form.method === "tools/call" && form.tool) bundle.tool = form.tool;
    if (form.method === "tools/call" && isObject(form.args)) bundle.args = clone(form.args);
    if (form.id !== undefined && form.id !== 1) bundle.id = form.id;
    var draft = buildRequestDraft({
      mode: bundle.mode, method: form.method, tool: bundle.tool || "", args: bundle.args || null, id: form.id
    }, options);
    var extra = {};
    var omit = [];
    var actual = actualHeaders || draft.headers;
    Object.keys(actual).forEach(function (name) {
      if (headerValue(draft.headers, name) !== String(actual[name]) || !headerValue(draft.headers, name)) extra[name] = String(actual[name]);
    });
    var omitNames = { "mcp-protocol-version": "version-header", "mcp-method": "method-header", "mcp-name": "name-header" };
    Object.keys(draft.headers).forEach(function (name) {
      if (headerValue(actual, name)) return;
      if (omitNames[name.toLowerCase()]) omit.push(omitNames[name.toLowerCase()]);
    });
    if (Object.keys(extra).length) bundle.headers = extra;
    if (omit.length) bundle.omit = omit;
    if (actualBodyText !== undefined && actualBodyText !== null) {
      var actualBody;
      var parsed = true;
      try { actualBody = JSON.parse(actualBodyText); } catch (error) { parsed = false; }
      if (!parsed) bundle.body = String(actualBodyText);
      else if (!deepEqual(actualBody, draft.body)) bundle.body = actualBody;
    }
    return bundle;
  }

  /* What the "형식 복사" button of the send screen copies: the request bundle
     format with two examples, to hand to an AI or a colleague. */
  function requestFormatText() {
    return [
      "MCP 테스트 요청 JSON 형식 (요청 보내기 화면에 붙여 넣으면 폼이 채워집니다)",
      "",
      "JSON 객체 하나입니다. method만 필수이고 나머지는 생략할 수 있습니다.",
      '- url: 보낼 주소. 생략하면 화면에 있는 주소를 그대로 씁니다. 예) "/mcp", "/mcp?cfg=<설정 코드>"',
      '- mode: "2026"(요청마다 버전을 싣는 2026-07-28 방식, 기본) 또는 "2025"(initialize로 시작하는 방식)',
      '- method: JSON-RPC 메서드. 예) "tools/list", "tools/call", "server/discover", "initialize", "ping"',
      "- tool: tools/call에서 부를 툴 이름",
      "- args: tools/call에서 툴에 넘길 인자(JSON 객체)",
      "- params: params를 통째로 지정할 때(_meta는 자동으로 합쳐집니다)",
      "- id: JSON-RPC id (기본 1)",
      '- headers: 더하거나 바꿀 요청 헤더. 예) {"Authorization": "Bearer <토큰>"}',
      "- omit: 일부러 빼고 보낼 것의 목록. " + REQUEST_OMIT.join(", "),
      "- body: 요청 바디를 그대로 지정할 때(JSON-RPC 객체). 주면 tool, args, params 대신 이 바디를 보냅니다",
      "",
      "예 1) 길이 변환 툴 호출",
      JSON.stringify({
        mode: "2026", method: "tools/call", tool: "convert_length",
        args: { value: 3, from_unit: "km", to_unit: "m" }
      }),
      "",
      "예 2) 버전 헤더를 뺀 잘못된 tools/list 요청",
      JSON.stringify({ mode: "2026", method: "tools/list", omit: ["version-header"] }),
      "",
      "JSON-RPC 요청 바디(" + '{"jsonrpc":"2.0","method":…}' + ")나 curl 명령을 그대로 붙여 넣어도 됩니다."
    ].join("\n");
  }

  /* Dotted paths of a partial config that the defaults do not have. */
  function unknownConfigPaths(patch, defaults) {
    var unknown = [];
    function walk(value, base, prefix) {
      Object.keys(value).forEach(function (key) {
        var path = prefix ? prefix + "." + key : key;
        if (!isObject(base) || !Object.prototype.hasOwnProperty.call(base, key)) unknown.push(path);
        else if (isObject(value[key]) && isObject(base[key])) walk(value[key], base[key], path);
      });
    }
    if (isObject(patch)) walk(patch, defaults, "");
    return unknown;
  }

  /* What a text pasted into "설정 불러오기" is: {ok, kind, patch, text, error}.
     kind "json": a config JSON (the part that differs from the defaults; a
     {"config": {...}} wrapper is taken off). kind "token": anything else, to
     be read as an address or a config code. */
  function parseConfigPaste(text, defaults) {
    var source = String(text === undefined || text === null ? "" : text).trim();
    if (!source) return { ok: false, kind: "", patch: null, text: "", error: "붙여 넣은 내용이 없습니다" };
    if (source[0] !== "{") return { ok: true, kind: "token", patch: null, text: source, error: "" };
    var value;
    try { value = JSON.parse(source); } catch (error) {
      return { ok: false, kind: "json", patch: null, text: source, error: jsonErrorLine(source, error) };
    }
    if (!isObject(value)) return { ok: false, kind: "json", patch: null, text: source, error: "JSON 객체({ … })여야 합니다" };
    if (isObject(value.config) && Object.keys(value).length === 1) value = value.config;
    var unknown = defaults ? unknownConfigPaths(value, defaults) : [];
    if (unknown.length) {
      return { ok: false, kind: "json", patch: value, text: source, error: "모르는 설정: " + unknown.join(", ") };
    }
    return { ok: true, kind: "json", patch: value, text: source, error: "" };
  }

  /* The config JSON a pasted config can be given as: the part of config that
     differs from the defaults (what the config code holds), pretty-printed. */
  function configJsonText(config, defaults) {
    return JSON.stringify(compactConfig(normalizeDerived(config), defaults), null, 2);
  }

  /* What the "형식 복사" button of the config screen copies. sections:
     [{id, label}] in display order. */
  function configFormatText(schema, sections, origin) {
    var lines = [
      "MCP 테스트 서버 설정 JSON 형식 (서버 설정 화면의 '설정 불러오기'에 붙여 넣습니다)",
      "",
      "기본값과 다르게 바꿀 항목만 담은 JSON 객체입니다. {}는 기본 설정입니다.",
      '경로의 점(.)마다 한 단계 중첩합니다. 예) toolCall.mode → {"toolCall": {"mode": "is-error"}}',
      "전체 옵션 정의(JSON): GET " + String(origin || "").replace(/\/+$/, "") + "/scenario/meta",
      "",
      "항목: 경로 (종류, 기본값) 이름 — 허용값"
    ];
    (sections || [{ id: "", label: "" }]).forEach(function (section) {
      if (section.label) lines.push("", "[" + section.label + "]");
      (schema || []).forEach(function (option) {
        if (section.id && option.section !== section.id) return;
        var allowed = "";
        if (Array.isArray(option.choices) && option.choices.length) {
          allowed = option.choices.map(function (choice) { return JSON.stringify(choice.value); }).join(" | ");
          if (option.kind === "list") allowed = "배열, 원소: " + allowed;
        } else if (typeof option.min === "number" && typeof option.max === "number") {
          allowed = option.min + "~" + option.max;
        } else if (option.kind === "bool") {
          allowed = "true | false";
        } else if (option.kind === "list") {
          allowed = "문자열 배열";
        } else if (option.kind === "json") {
          allowed = "배열, 원소: {" + (option.itemFields || []).join(", ") + "}";
        }
        lines.push(option.path + " (" + option.kind + ", " + JSON.stringify(option.default) + ") " + option.label +
          (allowed ? " — " + allowed : ""));
      });
    });
    lines.push(
      "",
      "예 1) 툴 호출이 isError로 답하는 서버",
      JSON.stringify({ toolCall: { mode: "is-error" } }),
      "",
      "예 2) OAuth 인증이 필요하고 툴 목록을 2개씩 나눠 주는 서버",
      JSON.stringify({ auth: { mode: "oauth" }, toolsList: { pageSize: 2 } })
    );
    return lines.join("\n");
  }

  /* The MCP addresses that got requests, for display. groups: what
     /inspect/log?groups=1 gives ([{cfg, count, screen, lastSeq,
     lastTimestamp}], cfg null for the address without a config). Returns
     [{cfg, plain, title, chips, readable, count, screen, lastSeq,
     lastTimestamp}], newest first. */
  function addressGroups(groups, defaults, schema) {
    return (groups || []).slice().sort(function (a, b) { return (b.lastSeq || 0) - (a.lastSeq || 0); }).map(function (group) {
      var plain = group.cfg === null || group.cfg === undefined || group.cfg === "";
      var summary = plain ? { ok: true, chips: [], text: "" } : tokenSummary(group.cfg, defaults, schema);
      return {
        cfg: plain ? "" : group.cfg,
        plain: plain,
        title: plain ? "기본 주소 (설정 없음)" : summary.text,
        chips: summary.chips,
        readable: summary.ok,
        count: group.count || 0,
        screen: group.screen || 0,
        lastSeq: group.lastSeq || 0,
        lastTimestamp: group.lastTimestamp || ""
      };
    });
  }

  return {
    PREVIEW_SOURCE: PREVIEW_SOURCE,
    MODERN_VERSION: MODERN_VERSION,
    REQUEST_OMIT: REQUEST_OMIT,
    jsonErrorPosition: jsonErrorPosition,
    jsonErrorLine: jsonErrorLine,
    parseCurl: parseCurl,
    headerValue: headerValue,
    mergeHeaders: mergeHeaders,
    guessRequestMode: guessRequestMode,
    parseRequestPaste: parseRequestPaste,
    paramHeaderValue: paramHeaderValue,
    buildRequestDraft: buildRequestDraft,
    requestBundle: requestBundle,
    requestFormatText: requestFormatText,
    unknownConfigPaths: unknownConfigPaths,
    parseConfigPaste: parseConfigPaste,
    configJsonText: configJsonText,
    configFormatText: configFormatText,
    addressGroups: addressGroups,
    SCENARIO_HEADER: SCENARIO_HEADER,
    clone: clone,
    deepEqual: deepEqual,
    getPath: getPath,
    setPath: setPath,
    applyPatch: applyPatch,
    compactConfig: compactConfig,
    normalizeDerived: normalizeDerived,
    encodeToken: encodeToken,
    decodeToken: decodeToken,
    formatValue: formatValue,
    optionState: optionState,
    diffSummary: diffSummary,
    patchToOverrides: patchToOverrides,
    mergeFormOverrides: mergeFormOverrides,
    localErrors: localErrors,
    buildMcpUrl: buildMcpUrl,
    scenarioHeaderLine: scenarioHeaderLine,
    buildRequest: buildRequest,
    buildCurl: buildCurl,
    buildCliCommand: buildCliCommand,
    matchOptionSearch: matchOptionSearch,
    classifyExchange: classifyExchange,
    isDelayLabel: isDelayLabel,
    requestMode: requestMode,
    filterBlockers: filterBlockers,
    filterExchanges: filterExchanges,
    describeConfigError: describeConfigError,
    maskSecretHeaders: maskSecretHeaders,
    formatLocalTime: formatLocalTime,
    formatLocalStamp: formatLocalStamp,
    toolName: toolName,
    tokenSummary: tokenSummary,
    tokenFromText: tokenFromText,
    defaultFilters: defaultFilters,
    filtersFromQuery: filtersFromQuery,
    filtersToQuery: filtersToQuery,
    decodeHeaderValue: decodeHeaderValue,
    headerBodyRows: headerBodyRows,
    buildExchangeCurl: buildExchangeCurl
  };
})();
if (typeof module !== "undefined") module.exports = UnitExpertUI;
"""


def _fill_template(template: str, parts: dict[str, str]) -> str:
    """Replace each ``__NAME__`` slot once. Inserted text is not scanned again."""
    return re.sub(r"__([A-Z0-9_]+?)__", lambda match: parts.get(match.group(1), match.group(0)), template)


UI_CORE_JS = _fill_template(
    _UI_CORE_JS_TEMPLATE,
    {
        "META_PROTOCOL_VERSION": META_PROTOCOL_VERSION,
        "META_CLIENT_CAPABILITIES": META_CLIENT_CAPABILITIES,
        "META_CLIENT_INFO": META_CLIENT_INFO,
        "MODERN_VERSION": PROTOCOL_VERSION_2026,
        "LEGACY_VERSION": LATEST_LEGACY_PROTOCOL_VERSION,
        "SCENARIO_HEADER": TEST_SCENARIO_HEADER,
        "PREVIEW_SOURCE": PREVIEW_SOURCE,
    },
)


def _script_json(value: Any) -> str:
    """JSON that is safe inside an inline <script>."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


# Shared look of the control pages (/scenario, /send, /inspect).
UI_BASE_CSS = """
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --surface: #ffffff;
      --soft: #e9eef7;
      --soft-line: #c8d1df;
      --text: #151922;
      --muted: #5f6875;
      --line: #dfe3ea;
      --accent: #0f766e;
      --accent-strong: #0b5f59;
      --accent-soft: #d7f2ee;
      --changed: #9a5b00;
      --changed-soft: #fff7ed;
      --changed-line: #fdba74;
      --danger: #b42318;
      --danger-soft: #fde7e4;
      --success: #146c43;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      font-size: 14px;
    }
    main { width: min(1440px, calc(100vw - 32px)); margin: 20px auto 56px; }
    h1 { margin: 0; font-size: 24px; font-weight: 700; line-height: 1.3; }
    h2 { margin: 0 0 8px; font-size: 17px; font-weight: 700; }
    h3 { margin: 0 0 6px; font-size: 14px; font-weight: 700; }
    p { margin: 0; }
    .lead { margin-top: 4px; color: var(--muted); font-size: 15px; }
    .hint { color: var(--muted); font-size: 12px; }
    .sr-only {
      position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
      overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
    }
    code, pre, textarea, .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    button, input, select, textarea { font: inherit; color: inherit; }
    button, a.button {
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #ffffff;
      border-radius: 6px;
      padding: 7px 12px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
      display: inline-block;
      text-align: center;
    }
    button:hover, a.button:hover { background: var(--accent-strong); }
    button.secondary, a.button.secondary { background: #ffffff; color: var(--accent-strong); border-color: var(--soft-line); }
    button.secondary:hover, a.button.secondary:hover { border-color: var(--accent); background: #ffffff; }
    button.secondary[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); }
    button.danger { background: #ffffff; color: var(--danger); border-color: #f0b4ad; }
    button.danger:hover { background: var(--danger-soft); }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    button.small { padding: 3px 9px; font-size: 12px; }
    .link-button {
      border: 0; background: none; padding: 0 2px; color: var(--accent-strong);
      font-size: 12px; font-weight: 600; text-decoration: underline;
    }
    .link-button:hover { background: none; color: var(--accent); }
    .link-button[hidden] { display: none; }
    input[type="text"], input[type="number"], input[type="search"], select, textarea {
      width: 100%;
      border: 1px solid var(--soft-line);
      border-radius: 6px;
      background: #ffffff;
      padding: 7px 9px;
      font-size: 13px;
    }
    textarea { resize: vertical; line-height: 1.45; }
    input[type="checkbox"] { width: 16px; height: 16px; margin: 0; accent-color: var(--accent); flex: none; }
    :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    a { color: var(--accent-strong); }
    .card { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 16px; min-width: 0; }
    .card[hidden] { display: none; }
    .card-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
    .card-head h2 { margin: 0; }
    .error-text { color: var(--danger); font-size: 12px; font-weight: 600; overflow-wrap: anywhere; }
    .error-text:empty { display: none; }
    .status-line { min-height: 18px; font-size: 12px; color: var(--success); font-weight: 600; }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; padding: 0; list-style: none; }
    .chip {
      display: inline-flex; align-items: center; gap: 4px; max-width: 100%;
      padding: 2px 9px; border-radius: 999px; font-size: 12px;
      border: 1px solid var(--changed-line); background: var(--changed-soft); overflow-wrap: anywhere;
    }
    .chip.is-ineffective { border-style: dashed; color: var(--muted); }
    /* A text that is copied: two lines until "전체 보기" opens it. */
    .copy-item { display: grid; gap: 4px; min-width: 0; }
    .copy-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .copy-label { font-size: 13px; font-weight: 600; }
    .copy-text {
      margin: 0; padding: 6px 8px; border-radius: 6px; background: #f1f3f7; border: 1px solid #e1e6ef; color: var(--text);
      white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.5; max-height: 50px; overflow: hidden;
    }
    .copy-text.is-open { max-height: 320px; overflow: auto; }
    .copy-text.is-empty { color: var(--muted); font-family: inherit; }
    .copy-expand { justify-self: start; }
"""

# Top navigation of the control pages. The colors fall back to fixed values,
# so the block looks the same whatever variables a page defines.
UI_NAV_CSS = """
    .ue-top {
      background: #ffffff;
      border-bottom: 1px solid var(--line, #dfe3ea);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .ue-nav {
      max-width: 1440px;
      margin: 0 auto;
      padding: 10px 20px;
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .ue-brand { font-size: 15px; font-weight: 700; margin-right: 14px; color: var(--text, #151922); }
    .ue-nav a {
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 14px;
      font-weight: 600;
      color: var(--muted, #5f6875);
      text-decoration: none;
    }
    .ue-nav a:hover { color: var(--accent-strong, #0b5f59); background: #eef4f3; }
    .ue-nav a[aria-current="page"] { color: #ffffff; background: var(--accent, #0f766e); }
    .ue-nav a:focus-visible { outline: 2px solid var(--accent, #0f766e); outline-offset: 2px; }
    .ue-host {
      margin-left: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      color: var(--muted, #5f6875);
    }
"""

NAV_ITEMS = (("scenario", "/scenario", "서버 설정"), ("send", "/send", "요청 보내기"), ("inspect", "/inspect", "요청 기록"))


def render_nav(current: str) -> str:
    """Top navigation shared by the control pages. current: "scenario" | "send" | "inspect"."""
    links = ""
    for key, href, label in NAV_ITEMS:
        current_attr = ' aria-current="page"' if key == current else ""
        links += f'<a href="{href}"{current_attr}>{label}</a>'
    return f"""<div class="ue-top">
    <nav class="ue-nav" aria-label="화면 이동">
      <strong class="ue-brand">{escape(SERVICE_NAME)} MCP</strong>
      {links}
      <span class="ue-host" id="ueHost"></span>
    </nav>
  </div>
  <script>document.getElementById("ueHost").textContent = location.host;</script>"""


def render_copy_item(key: str, label: str, copy_label: str | None = None) -> str:
    """One thing a page copies: its name, a copy button and the exact text."""
    return f"""
            <div class="copy-item" data-item="{key}">
              <div class="copy-head">
                <span class="copy-label">{escape(label)}</span>
                <button type="button" class="secondary small" data-copy="{key}" data-copy-label="{escape(copy_label or label)}">복사</button>
              </div>
              <pre class="copy-text" id="copy-{key}" tabindex="0"></pre>
              <button type="button" class="link-button copy-expand" data-expand="copy-{key}" aria-expanded="false" hidden>전체 보기</button>
            </div>"""


# ---------------------------------------------------------------------------
# /scenario page. The controls are rendered here from scenario_meta(); the
# page script keeps the state (base config + the paths the form changed) and
# updates what is shown.
# ---------------------------------------------------------------------------
APPLIES_TO_BADGES = {"modern": "2026-07-28 전용", "legacy": "2025 방식 전용"}


def _control_id(path: str) -> str:
    return path if path == "serverEra" else "opt-" + path.replace(".", "-")


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _default_text(option: dict[str, Any]) -> str:
    value = option["default"]
    if option["kind"] == "bool":
        return "켬" if value else "끔"
    if option["kind"] in {"list", "json"}:
        return ", ".join(str(item) for item in value) if value else "비움"
    return str(value)


def _choice_list(choices: list[dict[str, Any]]) -> str:
    items = ""
    for choice in choices:
        name = f'<strong>{escape(str(choice["label"]))}</strong> ' if choice.get("label") else ""
        items += (
            f'<li>{name}<code>{escape(str(choice["value"]))}</code> {escape(str(choice["description"]))}</li>'
        )
    return f'<ul class="choice-list">{items}</ul>'


def _option_text(choice: dict[str, Any]) -> str:
    """Visible text of one <option>: the plain name with the value, or the value with what it does."""
    value = str(choice["value"])
    if choice.get("label"):
        return f'{choice["label"]} ({value})'
    return f'{value} · {choice["description"]}'


def render_option(option: dict[str, Any]) -> str:
    """One option row: label, description (one line) and its control."""
    path = option["path"]
    kind = option["kind"]
    control_id = _control_id(path)
    desc_id = f"{control_id}-desc"
    attrs = f'id="{control_id}" data-config-path="{escape(path)}" aria-describedby="{desc_id}"'
    choices = option.get("choices")
    extra = ""
    wide = False
    label = f'<label class="opt-label" for="{control_id}">{escape(option["label"])}</label>'

    if kind == "bool":
        control = ""
        label = f'<input type="checkbox" {attrs} data-config-kind="bool">{label}'
    elif kind == "enum" and path == "serverEra":
        # tests/test_server_era.py pins this exact tag, so the select carries no
        # other attribute and the page script addresses it by id.
        options = "".join(
            f'<option value="{escape(str(choice["value"]))}">{escape(_option_text(choice))}</option>'
            for choice in choices
        )
        control = f'<select id="serverEra">{options}</select>'
        extra = _choice_list(choices)
        wide = True
    elif kind == "enum":
        options = "".join(
            f'<option value="{escape(str(choice["value"]))}">{escape(_option_text(choice))}</option>'
            for choice in choices
        )
        control = f'<select {attrs} data-config-kind="enum">{options}</select>'
        # Named choices are explained under the select; the others say what they do in the option itself.
        extra = _choice_list(choices) if any(choice.get("label") for choice in choices) else ""
    elif kind in {"int", "float"}:
        low, high = option.get("min"), option.get("max")
        bounds = "".join(
            f' {name}="{_number_text(bound)}"' for name, bound in (("min", low), ("max", high)) if bound is not None
        )
        step = "any" if kind == "float" else "1"
        control = (
            f'<input type="number" {attrs} data-config-kind="{kind}" step="{step}"{bounds} autocomplete="off">'
        )
        if low is not None and high is not None:
            extra = f'<p class="opt-range">{_number_text(low)}~{_number_text(high)}</p>'
    elif kind == "list" and choices:
        wide = len(choices) > 3
        label = f'<span class="opt-label" id="{control_id}-label">{escape(option["label"])}</span>'
        boxes = []
        for choice in choices:
            value = escape(str(choice["value"]))
            title = choice.get("label")
            detail = ""
            if title:
                detail = (
                    f'<span>{escape(title)}</span> <code>{value}</code>'
                    f'<span class="multi-desc">{escape(str(choice["description"]))}</span>'
                )
            else:
                detail = f"<code>{value}</code>"
            boxes.append(f'<label class="multi-item"><input type="checkbox" value="{value}">{detail}</label>')
        control = (
            f'<div class="multi" role="group" aria-labelledby="{control_id}-label" {attrs} '
            f'data-config-kind="multi">{"".join(boxes)}</div>'
        )
    elif kind == "list":
        control = f'<input type="text" {attrs} data-config-kind="list" autocomplete="off" spellcheck="false">'
        extra = '<p class="opt-range">쉼표로 구분</p>'
    elif kind == "json":
        wide = True
        control = (
            f'<textarea {attrs} data-config-kind="json" rows="6" spellcheck="false" autocomplete="off"></textarea>'
        )
        fields = ", ".join(option.get("itemFields") or [])
        extra = (
            f'<p class="opt-range">JSON 배열입니다. 규칙의 필드: {escape(fields)}. '
            'body 안의 <code>"{{id}}"</code>는 요청 id로 바뀝니다.</p>'
            f'<p class="opt-error" id="{control_id}-error" role="alert" hidden></p>'
        )
    else:
        suggestions = option.get("suggestions")
        list_attr = ""
        if suggestions:
            list_attr = f' list="{control_id}-list"'
            items = "".join(
                f'<option value="{escape(str(item["value"]))}">{escape(str(item["description"]))}</option>'
                for item in suggestions
            )
            extra = f'<datalist id="{control_id}-list">{items}</datalist>'
        control = (
            f'<input type="text" {attrs} data-config-kind="string"{list_attr} autocomplete="off" spellcheck="false">'
        )

    description = escape(option["description"])
    if description and not description.endswith((".", ")")):
        description += "."
    badge = APPLIES_TO_BADGES.get(option["appliesTo"])
    badge_html = f'<span class="era-badge">{escape(badge)}</span>' if badge else ""
    return f"""
        <div class="opt{' wide' if wide else ''}" data-opt="{escape(path)}" data-kind="{kind}">
          <div class="opt-head">
            {label}
            {badge_html}
            <span class="changed-mark" hidden>바꿈</span>
            <button type="button" class="link-button opt-reset" data-reset="{escape(path)}" hidden>되돌리기</button>
          </div>
          <p class="opt-desc" id="{desc_id}">{description} <span class="opt-default">기본값: {escape(_default_text(option))}</span> <code class="opt-key">{escape(path)}</code></p>
          {control}
          {extra}
          <p class="opt-reason" hidden></p>
        </div>"""


def _group_rows(meta: dict[str, Any], group_id: str) -> str:
    """Rows of one group. Rarely used ones (``detail``) go under "세부 설정"."""
    options = [option for option in meta["options"] if option["group"] == group_id]
    rows = "".join(render_option(option) for option in options if not option.get("detail"))
    details = [option for option in options if option.get("detail")]
    if details:
        rows += f"""
        <details class="more-opts" data-detail="{escape(group_id)}">
          <summary>세부 설정 {len(details)}개 <span class="group-badge" hidden></span></summary>
          <div class="group-body">{"".join(render_option(option) for option in details)}
          </div>
        </details>"""
    return rows


def render_option_groups(meta: dict[str, Any]) -> tuple[str, str]:
    """(build cards, error groups) in the order of meta["groups"].

    A build group is a card that is always open. An error group folds; the
    ones marked ``open`` start expanded.
    """
    build: list[str] = []
    error: list[str] = []
    for group in meta["groups"]:
        group_id = escape(group["id"])
        rows = _group_rows(meta, group["id"])
        if group["section"] == "build":
            build.append(
                f"""
      <section class="card group-card" data-group="{group_id}" aria-labelledby="g-{group_id}">
        <div class="group-head">
          <h3 id="g-{group_id}" class="group-title">{escape(group["label"])}</h3>
          <span class="group-badge" hidden></span>
          <span class="group-desc">{escape(group["description"])}</span>
        </div>
        <div class="group-body">{rows}
        </div>
      </section>"""
            )
            continue
        open_attr = ' open data-default-open="1"' if group.get("open") else ""
        error.append(
            f"""
      <details class="group" data-group="{group_id}"{open_attr}>
        <summary>
          <span class="group-title">{escape(group["label"])}</span>
          <span class="group-badge" hidden></span>
          <span class="group-desc">{escape(group["description"])}</span>
        </summary>
        <div class="group-body">{rows}
        </div>
      </details>"""
        )
    return "".join(build), "".join(error)


MATRIX_CELL_NOTES = {
    ("legacy", "legacy"): ("그대로 동작", False),
    ("legacy", "modern"): ("지원하지 않는 조합 (거절 확인용)", True),
    ("modern", "legacy"): ("중간에서 바꿔 줘야 동작", False),
    ("modern", "modern"): ("그대로 동작", False),
}


def render_presets(meta: dict[str, Any]) -> tuple[str, str]:
    """(compatibility matrix, error buttons).

    The matrix is request mode (rows) x what the server supports (columns).
    Both cells of a column set the same serverEra; only the client command
    differs. The buttons are the injection presets of the error section.
    """
    matrix = {
        (preset["clientMode"], preset["patch"]["serverEra"]): preset
        for preset in meta["presets"]
        if preset["category"] == "matrix"
    }
    eras = ("legacy", "modern")
    rows = []
    for client in eras:
        cells = []
        for server in eras:
            preset = matrix.get((client, server))
            if preset is None:
                cells.append("<td></td>")
                continue
            note, unsupported = MATRIX_CELL_NOTES[(client, server)]
            cells.append(
                f'<td><button type="button" class="matrix-cell{" unsupported" if unsupported else ""}" '
                f'data-preset="{escape(preset["id"])}" aria-pressed="false" aria-label="{escape(preset["label"])}: {escape(note)}">'
                f'<span class="matrix-note{"" if unsupported else " plain"}">{note}</span></button></td>'
            )
        rows.append(f'<tr><th scope="row">{REQUEST_MODE_LABELS[client]} 요청</th>{"".join(cells)}</tr>')
    head = "".join(f'<th scope="col">서버: {SERVER_ERA_LABELS[server]}</th>' for server in eras)
    injections = "".join(
        f'<button type="button" class="preset-button" data-preset="{escape(preset["id"])}" aria-pressed="false" '
        f'title="{escape(preset["description"])}">{escape(preset["label"])}</button>'
        for preset in meta["presets"]
        if preset["category"] == "injection" and preset["section"] == "error"
    )
    matrix_html = f"""
          <table class="matrix" aria-label="요청 방식과 서버의 조합">
            <thead><tr><th scope="col"><span class="sr-only">요청 방식</span></th>{head}</tr></thead>
            <tbody>{"".join(rows)}</tbody>
          </table>
          <div id="matrixDetail" class="matrix-detail" hidden>
            <p><strong>기대 결과</strong> <span id="matrixExpect"></span></p>
            {render_copy_item("matrixCli", "이 조합을 검사하는 명령", "검사 명령")}
          </div>"""
    injection_html = f"""
          <div class="preset-buttons" role="group" aria-labelledby="injectionTitle">{injections}</div>
          <p id="presetDetail" class="hint" aria-live="polite"></p>"""
    return matrix_html, injection_html


def render_scenario_page(message: str | None = None, error: str | None = None) -> str:
    meta = scenario_meta()
    build_groups, error_groups = render_option_groups(meta)
    notice = ""
    if message:
        notice = f'<div class="notice success" role="status">{escape(message)}</div>'
    if error:
        notice = f'<div class="notice error" role="alert">{escape(error)}</div>'
    matrix_html, injection_html = render_presets(meta)
    sections = {section["id"]: section for section in meta["sections"]}
    page_meta = {
        "defaults": meta["defaults"],
        "sections": meta["sections"],
        "groups": meta["groups"],
        "options": meta["options"],
        "presets": meta["presets"],
        "cliPrefix": CLIENT_COMMAND_PREFIX,
    }
    copy_items = "".join(
        render_copy_item(key, label, copy_label)
        for key, label, copy_label in (
            ("curlModern", "curl로 툴 목록 요청 (2026 방식)", "curl 명령(2026 방식)"),
            ("curlLegacy", "curl로 initialize 요청 (2025 방식)", "curl 명령(2025 방식)"),
            ("token", "설정 코드 (주소의 cfg= 뒤에 붙는 값)", "설정 코드"),
            ("header", "주소 대신 요청 헤더로 설정 넣기", "요청 헤더"),
            ("configJson", "설정 JSON", "설정 JSON"),
            ("cliProbe", "테스트 명령: 서버가 지원하는 스펙 확인", "테스트 명령(probe)"),
            ("cliRun", "테스트 명령: 전체 검사", "테스트 명령(run all)"),
        )
    )
    return _fill_template(
        _SCENARIO_PAGE,
        {
            "BASE_CSS": UI_BASE_CSS,
            "NAV_CSS": UI_NAV_CSS,
            "NAV": render_nav("scenario"),
            "NOTICE": notice,
            "BUILD_DESC": escape(sections["build"]["description"]),
            "ERROR_DESC": escape(sections["error"]["description"]),
            "MATRIX": matrix_html,
            "INJECTIONS": injection_html,
            "BUILD_GROUPS": build_groups,
            "ERROR_GROUPS": error_groups,
            "COPY_ITEMS": copy_items,
            "FORMAT_ITEM": render_copy_item("configFormat", "설정 JSON 형식 (AI에게 주고 설정을 만들게 할 때)", "설정 JSON 형식"),
            "UI_CORE_JS": UI_CORE_JS,
            "PAGE_JS": _fill_template(
                _SCENARIO_PAGE_JS,
                {"META_JSON": _script_json(page_meta), "GLOBAL_JSON": _script_json(get_active_config())},
            ),
        },
    )


_SCENARIO_PAGE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unit Expert MCP · 서버 설정</title>
  <style>
    __BASE_CSS__
    __NAV_CSS__
    code { background: #f1f3f7; border: 1px solid #e1e6ef; border-radius: 4px; padding: 1px 5px; overflow-wrap: anywhere; }
    .page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px 24px; flex-wrap: wrap; }
    .search-row { display: flex; align-items: center; gap: 8px; }
    .search-row label { font-size: 12px; color: var(--muted); white-space: nowrap; }
    .search-row input { width: 220px; padding: 5px 9px; }

    .banner, .notice {
      margin-top: 12px;
      padding: 10px 14px;
      border-radius: 8px;
      border: 1px solid var(--changed-line);
      background: var(--changed-soft);
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .banner[hidden] { display: none; }
    .banner span { flex: 1 1 320px; min-width: 0; overflow-wrap: anywhere; }
    .notice.success { border-color: #86efac; background: #ecfdf5; color: var(--success); }
    .notice.error { border-color: #f0b4ad; background: var(--danger-soft); color: var(--danger); }

    .layout {
      margin-top: 18px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(340px, 420px);
      gap: 20px;
      align-items: start;
    }
    .options-col { display: grid; gap: 28px; min-width: 0; }
    .part { display: grid; gap: 10px; min-width: 0; }
    .part[hidden] { display: none; }
    .part-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
    .part-head h2 { margin: 0; font-size: 19px; display: flex; align-items: center; gap: 8px; }
    .part-no {
      display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px;
      border-radius: 999px; background: var(--accent); color: #ffffff; font-size: 13px;
    }
    .part.error-part .part-no { background: var(--danger); }
    .part-desc { color: var(--muted); }
    .result-col {
      display: grid; gap: 10px; min-width: 0;
      position: sticky;
      top: 12px;
      max-height: calc(100vh - 24px);
      overflow: auto;
      scrollbar-gutter: stable;
    }
    .result-col .card.primary { background: var(--soft); border-color: var(--soft-line); }
    .card-desc { color: var(--muted); margin: -4px 0 10px; }
    .summary-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 6px 0; }
    .summary-head h3 { margin: 0; }
    .result-actions { display: grid; gap: 8px; margin-top: 14px; }
    .more > summary { cursor: pointer; font-weight: 700; font-size: 14px; }
    .more > summary .hint { font-weight: 400; margin-left: 6px; }
    .more-body { display: grid; gap: 14px; margin-top: 12px; }
    .more-body h3 { margin: 6px 0 0; padding-top: 12px; border-top: 1px solid var(--line); }
    .load-row { display: grid; gap: 6px; }
    .load-row button { justify-self: start; }

    .matrix { border-collapse: collapse; width: 100%; margin-top: 8px; table-layout: fixed; }
    .matrix th { font-size: 12px; color: var(--muted); font-weight: 600; padding: 4px 6px; text-align: left; }
    .matrix thead th:first-child, .matrix tbody th { width: 110px; }
    .matrix td { padding: 4px; }
    .matrix-cell {
      width: 100%; display: grid; gap: 1px; text-align: left; padding: 8px 10px;
      background: #ffffff; color: var(--text); border-color: var(--soft-line); font-weight: 600;
    }
    .matrix-note.plain { color: var(--text); }
    .matrix-cell:hover { background: #ffffff; border-color: var(--accent); }
    .matrix-cell[aria-pressed="true"], .preset-button[aria-pressed="true"] {
      background: var(--accent-soft); border-color: var(--accent); color: var(--accent-strong);
    }
    .matrix-cell.unsupported { border-style: dashed; }
    .matrix-note { font-size: 12px; font-weight: 600; color: var(--danger); }
    .matrix-detail { margin-top: 10px; padding: 10px 12px; border-radius: 6px; background: #f4f6fa; display: grid; gap: 8px; font-size: 13px; }
    .matrix-detail[hidden] { display: none; }
    .preset-buttons { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 6px; }
    .preset-button {
      background: #ffffff; color: var(--text); border-color: var(--soft-line);
      font-weight: 500; padding: 5px 10px; border-radius: 999px;
    }
    .preset-button:hover { background: #ffffff; border-color: var(--accent); }
    .preset-button[hidden] { display: none; }
    #presetDetail:empty { display: none; }

    .group, .group-card { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; min-width: 0; }
    .group[hidden], .group-card[hidden] { display: none; }
    .group-card { padding: 14px 16px 6px; }
    .group-card > .group-body > .opt { border-top: 0; padding-top: 10px; padding-bottom: 8px; }
    .group-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
    .group > summary {
      padding: 12px 16px; cursor: pointer; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
      border-radius: 8px;
    }
    .group-title { font-size: 15px; font-weight: 700; margin: 0; }
    .group-desc { color: var(--muted); font-size: 13px; flex: 1 1 200px; min-width: 0; }
    .group-badge {
      font-size: 11px; font-weight: 700; color: var(--changed); background: var(--changed-soft);
      border: 1px solid var(--changed-line); border-radius: 999px; padding: 1px 8px;
    }
    .group-badge[hidden] { display: none; }
    .group > .group-body { padding: 0 16px 8px; }
    .group-body { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 28px; }
    .more-opts { grid-column: 1 / -1; border-top: 1px solid var(--line); padding: 8px 0; }
    .more-opts[hidden] { display: none; }
    .more-opts > summary { cursor: pointer; font-size: 13px; font-weight: 600; color: var(--accent-strong); }
    .group.danger-zone { border-color: #f0b4ad; }
    .group.danger-zone .group-title { color: var(--danger); }
    .danger-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }
    .danger-zone .group-body, .compat .group-body { display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; padding-bottom: 14px; }
    .aux { display: grid; gap: 10px; }

    .opt { padding: 12px 0 12px 10px; border-top: 1px solid var(--line); border-left: 3px solid transparent; margin-left: -13px; display: grid; gap: 5px; align-content: start; min-width: 0; }
    .opt[hidden] { display: none; }
    .opt.wide { grid-column: 1 / -1; }
    .opt.is-changed { border-left-color: var(--changed-line); background: linear-gradient(90deg, var(--changed-soft), transparent 60%); }
    .opt.has-error { border-left-color: var(--danger); background: linear-gradient(90deg, var(--danger-soft), transparent 60%); }
    .opt-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .opt-label { font-weight: 600; }
    .opt-desc { color: var(--muted); font-size: 13px; }
    .opt-default { color: #7a8494; font-size: 12px; overflow-wrap: anywhere; }
    .opt-key { color: #8a93a1; font-size: 11px; background: none; border: 0; padding: 0; }
    .opt-range, .choice-desc { color: var(--muted); font-size: 12px; }
    .choice-desc:empty { display: none; }
    .opt-reason { color: var(--changed); font-size: 12px; font-weight: 600; }
    .opt-error { color: var(--danger); font-size: 12px; font-weight: 600; overflow-wrap: anywhere; }
    .error-text { white-space: pre-line; }
    #urlError:not(:empty) { margin-top: 6px; }
    .opt.is-ineffective > :not(.opt-reason) { opacity: 0.55; }
    .era-badge {
      font-size: 11px; font-weight: 700; color: var(--accent-strong); background: var(--accent-soft);
      border-radius: 6px; padding: 1px 7px;
    }
    .changed-mark {
      font-size: 11px; font-weight: 700; color: var(--changed); background: var(--changed-soft);
      border: 1px solid var(--changed-line); border-radius: 6px; padding: 0 6px;
    }
    .choice-list { margin: 2px 0 0; padding: 0; list-style: none; display: grid; gap: 3px; font-size: 13px; color: var(--muted); }
    .choice-list strong { color: var(--text); }
    .multi { display: grid; gap: 6px; }
    .multi-item { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; cursor: pointer; }
    .multi-item input { align-self: center; }
    .multi-desc { flex-basis: 100%; padding-left: 24px; color: var(--muted); font-size: 12px; }
    .opt[data-kind="int"] input, .opt[data-kind="float"] input { max-width: 200px; }
    .opt select { max-width: 560px; }

    .url-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
    .url-row input { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .chip { padding: 2px 4px 2px 9px; }
    .chip-text { overflow-wrap: anywhere; }
    .chip-remove {
      border: 0; background: none; color: var(--changed); padding: 0; font-size: 15px; line-height: 1; border-radius: 999px;
      min-width: 24px; min-height: 24px; display: inline-flex; align-items: center; justify-content: center; flex: none;
    }
    .chip-remove:hover { background: var(--changed-line); color: var(--text); }
    @media (max-width: 980px) {
      /* One column. The address card (address, copy, changed settings) stays
         at the top of the window while the options scroll under it; "더 보기"
         follows the options. */
      .layout { display: flex; flex-direction: column; align-items: stretch; gap: 16px; }
      .result-col { display: contents; }
      .result-col .card { order: 2; }
      .result-col .card.primary {
        order: -1; position: sticky; top: 0; z-index: 5; padding: 10px 14px;
        box-shadow: 0 6px 14px rgba(21, 25, 34, 0.14);
      }
      .result-col .card.primary .card-desc { display: none; }
      .result-col .card.primary .chips { max-height: 58px; overflow: auto; }
      .result-col .card.primary #urlError { max-height: 74px; overflow: auto; }
      .result-col .card.primary .result-actions { grid-template-columns: 1fr 1fr; margin-top: 8px; }
    }
    @media (max-width: 620px) {
      .group-body { grid-template-columns: minmax(0, 1fr); }
      .search-row input { width: 100%; }
      .search-row { flex: 1 1 100%; }
    }
  </style>
</head>
<body>
  __NAV__
  <main>
    <div class="page-head">
      <div>
        <h1>서버 설정</h1>
        <p class="lead">원하는 모양의 테스트용 MCP 서버를 만들고, 필요하면 일부러 에러를 내게 합니다.</p>
      </div>
      <div class="search-row">
        <label for="optionSearch">설정 찾기</label>
        <input type="search" id="optionSearch" placeholder="이름이나 설명" autocomplete="off">
        <span id="searchCount" class="hint" aria-live="polite"></span>
      </div>
    </div>
    <div id="globalBanner" class="banner" role="status" hidden>
      <span id="globalBannerText"></span>
      <button type="button" class="danger" id="globalBannerReset">서버 기본값 되돌리기</button>
    </div>
    __NOTICE__
    <div class="layout">
      <div class="options-col" id="optionsCol">
        <section class="part" id="buildPart" aria-labelledby="buildTitle">
          <div class="part-head">
            <h2 id="buildTitle"><span class="part-no" aria-hidden="true">1</span>서버 만들기</h2>
            <p class="part-desc">__BUILD_DESC__.</p>
          </div>
          __BUILD_GROUPS__
        </section>

        <section class="part error-part" id="errorPart" aria-labelledby="errorTitle">
          <div class="part-head">
            <h2 id="errorTitle"><span class="part-no" aria-hidden="true">2</span>에러 넣기</h2>
            <p class="part-desc">__ERROR_DESC__.</p>
          </div>
          <section class="card" id="injectionCard" aria-labelledby="injectionTitle">
            <h3 id="injectionTitle">자주 쓰는 에러</h3>
            __INJECTIONS__
          </section>
          __ERROR_GROUPS__
        </section>

        <div class="aux">
          <details class="group compat" id="compatBox">
            <summary>
              <span class="group-title">2026-07-28 호환 테스트</span>
              <span class="group-desc">요청 방식과 서버의 조합을 골라 검사 명령을 받습니다</span>
            </summary>
            <div class="group-body">
              __MATRIX__
            </div>
          </details>
          <details class="group danger-zone" id="dangerZone">
            <summary>
              <span class="group-title">서버 기본값 바꾸기</span>
              <span class="group-desc">모든 사용자에게 적용됩니다</span>
            </summary>
            <div class="group-body">
              <p>주소에 설정이 없는 모든 요청의 동작을 바꿉니다. 서버를 다시 시작하면 처음으로 돌아갑니다.</p>
              <p id="globalState" class="hint"></p>
              <div class="danger-actions">
                <button type="button" class="danger" id="applyGlobal">지금 설정을 서버 기본값으로</button>
                <button type="button" class="danger" id="resetGlobal">서버 기본값 되돌리기</button>
                <button type="button" class="danger" id="expireSessions">세션 모두 끊기</button>
              </div>
              <p id="dangerStatus" class="status-line" aria-live="polite"></p>
            </div>
          </details>
        </div>
      </div>

      <aside class="result-col" aria-label="내 MCP 서버 주소">
        <section class="card primary" id="resultCard" aria-labelledby="urlTitle">
          <h2 id="urlTitle"><label for="mcpUrl">내 MCP 서버 주소</label></h2>
          <p class="card-desc">이 주소로 연결하면 왼쪽 설정대로 동작합니다.</p>
          <div class="url-row">
            <input type="text" id="mcpUrl" readonly spellcheck="false" placeholder="오류를 고치면 주소가 만들어집니다">
            <button type="button" id="copyUrl" data-copy="url" data-copy-label="주소">주소 복사</button>
          </div>
          <p id="urlError" class="error-text" role="alert"></p>
          <button type="button" class="link-button" id="urlErrorJump" hidden>오류가 난 곳으로 이동</button>
          <p id="copyStatus" class="status-line" aria-live="polite"></p>
          <div class="summary-head">
            <h3 id="chipsTitle">바꾼 설정</h3>
            <button type="button" class="secondary small" id="resetAll">모두 되돌리기</button>
          </div>
          <p id="chipsEmpty" class="hint">바꾼 설정이 없습니다. 기본 서버입니다.</p>
          <ul id="chips" class="chips" aria-labelledby="chipsTitle"></ul>
          <div class="result-actions">
            <a class="button" id="inspectLink" href="/inspect">이 주소로 온 요청 보기</a>
            <a class="button secondary" id="sendLink" href="/send">이 주소로 요청 보내 보기</a>
          </div>
        </section>

        <details class="card more" id="moreBox">
          <summary>더 보기<span class="hint">curl · 설정 코드 · 테스트 명령 · 설정 불러오기</span></summary>
          <div class="more-body">
            <p id="moreNote" class="hint" hidden></p>
            __COPY_ITEMS__
            <p class="hint">테스트 명령은 이 저장소의 루트에서 실행합니다.</p>
            <h3 id="loadTitle"><label for="loadToken">설정 불러오기</label></h3>
            <div class="load-row">
              <textarea id="loadToken" rows="3" autocomplete="off" spellcheck="false" placeholder="MCP 주소, 설정 코드, 또는 설정 JSON을 붙여 넣으세요"></textarea>
              <button type="button" class="secondary" id="loadTokenButton">불러오기</button>
              <p id="loadError" class="error-text" role="alert"></p>
              <p id="loadStatus" class="status-line" aria-live="polite"></p>
            </div>
            __FORMAT_ITEM__
          </div>
        </details>
      </aside>
    </div>
  </main>
  <script>
__UI_CORE_JS__
  </script>
  <script>
__PAGE_JS__
  </script>
</body>
</html>"""


_SCENARIO_PAGE_JS = r"""(function () {
  "use strict";
  const UI = UnitExpertUI;
  const META = __META_JSON__;
  let globalConfig = __GLOBAL_JSON__;
  const DEFAULTS = META.defaults;
  const SCHEMA = META.options;
  const OPTIONS = {};
  SCHEMA.forEach((option) => { OPTIONS[option.path] = option; });
  const PRESETS = {};
  META.presets.forEach((preset) => { PRESETS[preset.id] = preset; });
  const GROUP_SECTION = {};
  META.groups.forEach((group) => { GROUP_SECTION[group.id] = group.section; });
  const CONFIG_FORMAT = UI.configFormatText(SCHEMA, META.sections, location.origin);
  const GATEWAY_URL_SLOT = "<게이트웨이 MCP 주소>";
  const BLOCKED_TEXT = "오류를 고치면 만들어집니다.";
  // Why a copy item has nothing to copy while the config is the default one.
  const DEFAULT_ONLY = {
    token: "바꾼 설정이 없어 설정 코드가 없습니다. 기본 주소만 쓰면 됩니다.",
    header: "바꾼 설정이 없어 넣을 헤더가 없습니다."
  };

  // The config on screen is base (the defaults or a loaded token) with the
  // paths the form changed written over it. Fields without a control stay as
  // they are in base.
  const state = {
    base: UI.clone(DEFAULTS),
    overrides: {},
    serverToken: null,
    serverError: "",
    // True from a change until the server has answered whether it is valid.
    validating: false,
    // The last token known to be valid: what #cfg and the record link carry.
    goodToken: "",
    jsonErrors: {},
    matrixPreset: null,
    restoring: false,
    validateSeq: 0,
    validateTimer: null,
    groupChanged: {},
    copyValues: {},
    errorPath: ""
  };

  const byId = (id) => document.getElementById(id);
  const optionsCol = byId("optionsCol");

  function currentConfig() {
    return UI.mergeFormOverrides(state.base, state.overrides);
  }

  function advancedInputs() {
    return Array.from(document.querySelectorAll("[data-config-path]"));
  }

  function splitList(text) {
    const items = [];
    String(text).split(",").forEach((part) => {
      const item = part.trim();
      if (item && !items.includes(item)) items.push(item);
    });
    return items;
  }

  // Value of one control. undefined when the text cannot be read (broken JSON).
  function readAdvanced(input) {
    const path = input.dataset.configPath;
    const kind = input.dataset.configKind;
    const option = OPTIONS[path];
    if (kind === "bool") return input.checked;
    if (kind === "multi") {
      return Array.from(input.querySelectorAll('input[type="checkbox"]'))
        .filter((box) => box.checked)
        .map((box) => box.value);
    }
    if (kind === "list") return splitList(input.value);
    if (kind === "json") {
      const text = input.value.trim();
      if (!text) {
        delete state.jsonErrors[path];
        return UI.clone(option.default);
      }
      try {
        const value = JSON.parse(text);
        delete state.jsonErrors[path];
        return value;
      } catch (error) {
        state.jsonErrors[path] = UI.jsonErrorLine(text, error);
        return undefined;
      }
    }
    if (kind === "int" || kind === "float") {
      const text = input.value.trim();
      const number = Number(text);
      return text === "" || !Number.isFinite(number) ? UI.clone(option.default) : number;
    }
    if (kind === "enum") return typeof option.default === "number" ? Number(input.value) : input.value;
    const text = input.value.trim();
    return text === "" ? UI.clone(option.default) : text;
  }

  function writeAdvanced(input, config) {
    const kind = input.dataset.configKind;
    const value = UI.getPath(config, input.dataset.configPath);
    if (kind === "bool") {
      input.checked = value === true;
    } else if (kind === "multi") {
      const selected = Array.isArray(value) ? value : [];
      input.querySelectorAll('input[type="checkbox"]').forEach((box) => {
        box.checked = selected.includes(box.value);
      });
    } else if (kind === "list") {
      input.value = Array.isArray(value) ? value.join(", ") : "";
    } else if (kind === "json") {
      input.value = Array.isArray(value) && !value.length ? "" : JSON.stringify(value, null, 2);
      delete state.jsonErrors[input.dataset.configPath];
    } else {
      input.value = value === undefined || value === null ? "" : String(value);
    }
  }

  function readForm() {
    const values = { serverEra: document.getElementById("serverEra").value };
    advancedInputs().forEach((input) => {
      const value = readAdvanced(input);
      if (value !== undefined) values[input.dataset.configPath] = value;
    });
    return values;
  }

  function writeForm(config) {
    document.getElementById("serverEra").value = config.serverEra;
    advancedInputs().forEach((input) => writeAdvanced(input, config));
  }

  // Record as overrides only what the form shows differently from the config.
  function syncFromForm() {
    const config = currentConfig();
    const values = readForm();
    Object.keys(values).forEach((path) => {
      if (!UI.deepEqual(values[path], UI.getPath(config, path))) state.overrides[path] = values[path];
    });
  }

  function optionTitle(path) {
    const option = OPTIONS[path];
    return option ? option.label + " (" + path + ")" : path;
  }

  // Problems the form can tell without the server: [{path, text}].
  function localProblems(config) {
    const list = UI.localErrors(config, SCHEMA).map((entry) => ({
      path: entry.path,
      text: "설정 오류: " + optionTitle(entry.path) + " — " + entry.message.replace(entry.path + "는 ", "")
    }));
    Object.keys(state.jsonErrors).forEach((path) => {
      list.push({ path: path, text: "설정 오류: " + optionTitle(path) + " — " + state.jsonErrors[path] });
    });
    return list;
  }

  // Every problem that blocks the URL, one entry per line on screen.
  function problems(config) {
    const list = localProblems(config);
    if (!list.length && state.serverError) {
      const described = UI.describeConfigError(state.serverError, SCHEMA);
      list.push({ path: described.option ? described.option.path : "", text: described.text });
    }
    return list;
  }

  function currentToken(config) {
    return state.serverToken !== null ? state.serverToken : UI.encodeToken(config, DEFAULTS, SCHEMA);
  }

  function setText(id, text) {
    const node = byId(id);
    if (node.textContent !== text) node.textContent = text;
  }

  // Shows the "전체 보기" button of every copy text that does not fit in its
  // two lines. A text inside a closed <details> has no height yet, so this
  // also runs when one opens.
  function syncExpandButtons() {
    document.querySelectorAll("[data-expand]").forEach((button) => {
      const text = byId(button.dataset.expand);
      const open = text.classList.contains("is-open");
      button.hidden = !open && !(text.scrollHeight > text.clientHeight + 1);
      button.setAttribute("aria-expanded", open ? "true" : "false");
      const label = open ? "접기" : "전체 보기";
      if (button.textContent !== label) button.textContent = label;
    });
  }

  // A copy item always shows the exact text its button copies. With nothing
  // to copy it says why, and the button is off.
  function writeCopyText(key, value, why) {
    const node = byId("copy-" + key);
    if (!node) return;
    const text = value || why;
    if (node.textContent !== text) node.textContent = text;
    node.classList.toggle("is-empty", !value);
  }

  function matrixCli(preset) {
    return preset.cli.replace("{url}", GATEWAY_URL_SLOT);
  }

  function renderResult(config, token, blocked, pending) {
    const url = UI.buildMcpUrl(location.origin, token);
    const good = state.goodToken;
    byId("mcpUrl").value = blocked ? "" : url;
    const goodQuery = good ? "?cfg=" + encodeURIComponent(good) : "";
    byId("inspectLink").href = "/inspect" + goodQuery;
    byId("sendLink").href = "/send" + goodQuery;
    const matrix = state.matrixPreset ? PRESETS[state.matrixPreset] : null;
    const values = {
      url: url,
      token: token,
      header: UI.scenarioHeaderLine(token),
      curlLegacy: UI.buildCurl("legacy-initialize", url),
      curlModern: UI.buildCurl("modern-tools-list", url),
      configJson: UI.configJsonText(config, DEFAULTS),
      cliProbe: UI.buildCliCommand("probe", url, { prefix: META.cliPrefix }),
      cliRun: UI.buildCliCommand("run all", url, { prefix: META.cliPrefix, expect: "server" })
    };
    // With an error there is no valid address, so nothing that carries it is offered.
    if (blocked) Object.keys(values).forEach((key) => { values[key] = ""; });
    values.matrixCli = matrix ? matrixCli(matrix) : "";
    values.configFormat = CONFIG_FORMAT;
    state.copyValues = values;
    Object.keys(values).forEach((key) => {
      if (key !== "url") writeCopyText(key, values[key], blocked ? BLOCKED_TEXT : DEFAULT_ONLY[key] || "");
    });
    const note = byId("moreNote");
    note.hidden = blocked || Boolean(token);
    setText("moreNote", "바꾼 설정이 없어 기본 주소만 있습니다. 설정 코드와 헤더는 설정을 바꾸면 생깁니다.");
    document.querySelectorAll("[data-copy]").forEach((button) => {
      const key = button.dataset.copy;
      button.disabled = !values[key] || (pending && key !== "matrixCli" && key !== "configFormat");
    });
    syncExpandButtons();
  }

  const chipNodes = {};

  function makeChip(path) {
    const item = document.createElement("li");
    item.dataset.chip = path;
    const text = document.createElement("span");
    text.className = "chip-text";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "chip-remove";
    remove.dataset.reset = path;
    remove.textContent = "×";
    item.append(text, remove);
    return { item: item, text: text, remove: remove };
  }

  // Chips are kept by config path: a chip that stays is the same node, so a
  // press that started on its button still ends on it.
  function renderChips(config) {
    const chips = UI.diffSummary(config, DEFAULTS, SCHEMA);
    const list = byId("chips");
    const wanted = {};
    let previous = null;
    chips.forEach((chip) => {
      wanted[chip.path] = true;
      let node = chipNodes[chip.path];
      const isNew = !node;
      if (isNew) node = chipNodes[chip.path] = makeChip(chip.path);
      const className = "chip" + (chip.effective ? "" : " is-ineffective");
      if (node.item.className !== className) node.item.className = className;
      node.item.title = chip.reason || "";
      const label = chip.label + " = " + chip.value + (chip.effective ? "" : " (적용 안 됨)");
      if (node.text.textContent !== label) node.text.textContent = label;
      node.remove.setAttribute("aria-label", chip.label + " 되돌리기");
      const expected = previous ? previous.nextSibling : list.firstChild;
      if (isNew || expected !== node.item) {
        if (typeof list.insertBefore === "function") list.insertBefore(node.item, expected || null);
        else list.append(node.item);
      }
      previous = node.item;
    });
    Object.keys(chipNodes).forEach((path) => {
      if (wanted[path]) return;
      chipNodes[path].item.remove();
      delete chipNodes[path];
    });
    byId("chipsEmpty").hidden = chips.length > 0;
    byId("resetAll").disabled = chips.length === 0;
  }

  function setBadge(badge, count) {
    if (!badge) return;
    badge.hidden = count === 0;
    badge.textContent = "바꿈 " + count;
  }

  function renderOptions(config, errorPaths) {
    const changedByGroup = {};
    const changedDetail = {};
    document.querySelectorAll(".opt").forEach((row) => {
      const option = OPTIONS[row.dataset.opt];
      const value = UI.getPath(config, option.path);
      const changed = !UI.deepEqual(value, option.default);
      if (changed) changedByGroup[option.group] = (changedByGroup[option.group] || 0) + 1;
      if (changed && option.detail) changedDetail[option.group] = (changedDetail[option.group] || 0) + 1;
      row.classList.toggle("is-changed", changed);
      row.classList.toggle("has-error", errorPaths.indexOf(option.path) >= 0);
      row.querySelector(".changed-mark").hidden = !changed;
      row.querySelector(".opt-reset").hidden = !changed;
      const optionState = UI.optionState(option, config, SCHEMA);
      row.classList.toggle("is-ineffective", !optionState.effective);
      const reason = row.querySelector(".opt-reason");
      reason.hidden = optionState.effective;
      reason.textContent = optionState.reason;
      const error = row.querySelector(".opt-error");
      if (error) {
        error.hidden = !state.jsonErrors[option.path];
        error.textContent = state.jsonErrors[option.path] || "";
      }
    });
    document.querySelectorAll("[data-group]").forEach((group) => {
      const count = changedByGroup[group.dataset.group] || 0;
      setBadge(group.querySelector(".group-badge"), count);
      // Open a group when it first gets a change. The user may close it again.
      if (count > 0 && !state.groupChanged[group.dataset.group]) group.open = true;
      state.groupChanged[group.dataset.group] = count > 0;
    });
    // The rarely used options of a group ("세부 설정") open the same way.
    document.querySelectorAll("[data-detail]").forEach((box) => {
      const key = "detail:" + box.dataset.detail;
      const count = changedDetail[box.dataset.detail] || 0;
      setBadge(box.querySelector(".group-badge"), count);
      if (count > 0 && !state.groupChanged[key]) box.open = true;
      state.groupChanged[key] = count > 0;
    });
  }

  function patchApplies(config, patch) {
    const overrides = UI.patchToOverrides(patch, SCHEMA);
    return Object.keys(overrides).every((path) => UI.deepEqual(UI.getPath(config, path), overrides[path]));
  }

  function presetActive(preset, config) {
    return preset.category === "matrix" ? state.matrixPreset === preset.id : patchApplies(config, preset.patch);
  }

  function renderPresets(config) {
    const matrix = state.matrixPreset ? PRESETS[state.matrixPreset] : null;
    if (matrix && !patchApplies(config, matrix.patch)) state.matrixPreset = null;
    if (state.matrixPreset) byId("compatBox").open = true;
    document.querySelectorAll("[data-preset]").forEach((button) => {
      button.setAttribute("aria-pressed", presetActive(PRESETS[button.dataset.preset], config) ? "true" : "false");
    });
    const selected = state.matrixPreset ? PRESETS[state.matrixPreset] : null;
    byId("matrixDetail").hidden = !selected;
    if (selected) setText("matrixExpect", selected.expect);
  }

  function renderGlobal() {
    const chips = UI.diffSummary(globalConfig, DEFAULTS, SCHEMA);
    const text = chips.map((chip) => chip.label + " = " + chip.value).join(", ");
    byId("globalBanner").hidden = chips.length === 0;
    setText("globalBannerText", "서버 기본값이 바뀌어 있습니다(모든 사용자에게 적용): " + text);
    setText("globalState", chips.length ? "지금 서버 기본값: " + text : "지금 서버 기본값: 처음 그대로");
    byId("resetGlobal").disabled = chips.length === 0;
  }

  function updateHash(token) {
    if (state.restoring) return;
    const target = token ? "#cfg=" + token : "";
    if (location.hash !== target) {
      history.replaceState(null, "", location.pathname + location.search + target);
    }
  }

  function renderErrors(list) {
    const withPath = list.filter((entry) => entry.path && OPTIONS[entry.path])[0];
    state.errorPath = withPath ? withPath.path : "";
    const lines = list.map((entry) => entry.text);
    if (lines.length) lines.push("오류를 고치면 주소가 만들어집니다.");
    setText("urlError", lines.join("\n"));
    const jump = byId("urlErrorJump");
    jump.hidden = !withPath;
    if (withPath) setText("urlErrorJump", "오류가 난 곳으로 이동: " + OPTIONS[withPath.path].label);
  }

  function render() {
    const config = currentConfig();
    const list = problems(config);
    const blocked = list.length > 0;
    const pending = state.validating;
    const token = currentToken(config);
    // #cfg and the record link only ever carry a token the server accepted.
    if (!blocked && !pending) state.goodToken = token;
    renderPresets(config);
    renderResult(config, token, blocked, pending);
    renderChips(config);
    renderOptions(config, list.map((entry) => entry.path));
    renderErrors(list);
    if (!blocked && !pending) updateHash(token);
  }

  async function postScenario(payload) {
    const response = await fetch("/scenario", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    let body = null;
    try {
      body = await response.json();
    } catch (error) {
      body = { ok: false, error: "서버 응답을 읽을 수 없습니다 (HTTP " + response.status + ")" };
    }
    return body;
  }

  // Ask the server whether the config is valid, shortly after the last change.
  function scheduleValidate() {
    clearTimeout(state.validateTimer);
    const seq = ++state.validateSeq;
    state.validateTimer = setTimeout(async () => {
      let body;
      try {
        body = await postScenario({ action: "validate", config: currentConfig() });
      } catch (error) {
        // The server cannot be asked: go on with the token made here.
        body = { ok: true, token: null };
      }
      if (seq !== state.validateSeq) return;
      state.validating = false;
      if (body.ok) {
        state.serverToken = typeof body.token === "string" ? body.token : null;
        state.serverError = "";
      } else {
        state.serverError = body.error || "설정을 확인하지 못했습니다";
      }
      render();
    }, 300);
  }

  // After every change of the config. An earlier server error stays on screen
  // until the server has looked at the new config, and nothing can be copied
  // in between.
  function changed() {
    state.serverToken = null;
    if (localProblems(currentConfig()).length) {
      clearTimeout(state.validateTimer);
      state.validateSeq += 1;
      state.validating = false;
      state.serverError = "";
      render();
      return;
    }
    state.validating = true;
    render();
    scheduleValidate();
  }

  // A change event arrives while the mouse button is still down when the
  // press is what took the focus out of the field. Drawing the page again at
  // that moment can move the pressed button from under the pointer and the
  // click is lost, so the work waits until the press is over.
  let pointerIsDown = false;
  let afterPointer = [];
  function whenPointerIsUp(work) {
    if (pointerIsDown) afterPointer.push(work);
    else work();
  }
  function pointerReleased() {
    pointerIsDown = false;
    if (!afterPointer.length) return;
    const waiting = afterPointer;
    afterPointer = [];
    // After the click that belongs to this press.
    setTimeout(() => { waiting.forEach((work) => work()); }, 0);
  }
  document.addEventListener("pointerdown", () => { pointerIsDown = true; }, true);
  document.addEventListener("pointerup", pointerReleased, true);
  document.addEventListener("pointercancel", pointerReleased, true);
  window.addEventListener("blur", pointerReleased);

  function controlOf(path) {
    if (path === "serverEra") return byId("serverEra");
    const control = document.querySelector('[data-config-path="' + path + '"]');
    if (control && control.dataset.configKind === "multi") return control.querySelector("input") || control;
    return control;
  }

  function revealOption(path) {
    const control = controlOf(path);
    if (!control) return;
    // Open everything the control sits in: "세부 설정", then its group.
    let box = control.closest("details");
    while (box) {
      box.hidden = false;
      box.open = true;
      box = box.parentElement ? box.parentElement.closest("details") : null;
    }
    const card = control.closest("[data-group]");
    if (card) card.hidden = false;
    const row = control.closest(".opt");
    if (row) row.hidden = false;
    (row || control).scrollIntoView({ block: "center" });
    control.focus({ preventScroll: true });
  }

  function resetPath(path) {
    state.overrides[path] = UI.clone(UI.getPath(DEFAULTS, path));
    writeForm(currentConfig());
    changed();
  }

  function loadConfig(config, token) {
    state.base = config;
    state.overrides = {};
    state.matrixPreset = null;
    writeForm(currentConfig());
    if (typeof token === "string" && !localProblems(currentConfig()).length) {
      // The server has just normalized this config: nothing to ask again.
      clearTimeout(state.validateTimer);
      state.validateSeq += 1;
      state.serverToken = token;
      state.serverError = "";
      state.validating = false;
      render();
      return;
    }
    changed();
  }

  // A preset button is a toggle: pressing an applied one puts its options
  // back to the defaults.
  function applyPreset(preset) {
    const overrides = UI.patchToOverrides(preset.patch, SCHEMA);
    if (presetActive(preset, currentConfig())) {
      Object.keys(overrides).forEach((path) => {
        state.overrides[path] = UI.clone(UI.getPath(DEFAULTS, path));
      });
      if (preset.category === "matrix") state.matrixPreset = null;
      setText("presetDetail", preset.category === "injection" ? "뺐습니다: " + preset.label : "");
    } else {
      Object.assign(state.overrides, overrides);
      if (preset.category === "matrix") state.matrixPreset = preset.id;
      setText(
        "presetDetail",
        preset.category === "injection" ? "넣었습니다: " + preset.description : ""
      );
    }
    writeForm(currentConfig());
    changed();
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.setAttribute("aria-hidden", "true");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    const done = document.execCommand("copy");
    area.remove();
    if (!done) throw new Error("copy failed");
  }

  async function handleCopy(button) {
    const text = state.copyValues[button.dataset.copy];
    if (!text) return;
    const label = button.dataset.copyLabel;
    const original = button.dataset.originalText || button.textContent;
    button.dataset.originalText = original;
    try {
      await copyText(text);
      button.textContent = "복사됨";
      setText("copyStatus", "복사했습니다: " + label);
    } catch (error) {
      setText("copyStatus", "복사하지 못했습니다. 글자를 직접 선택해 복사하세요.");
    }
    setTimeout(() => { button.textContent = original; }, 1400);
  }

  // The server message (English, part of the API) follows a Korean explanation.
  const LOAD_ERRORS = {
    "invalid config token": "주소나 설정 코드를 읽지 못했습니다. 잘린 곳이 없는지 확인하세요.",
    "empty config token": "붙여 넣은 내용이 없습니다.",
    "token must be a non-empty string": "붙여 넣은 내용이 없습니다.",
    "no cfg parameter in the URL": "붙여 넣은 주소에 설정(cfg=…)이 없습니다."
  };

  function loadErrorText(message) {
    if (!message) return "설정을 불러오지 못했습니다.";
    return (LOAD_ERRORS[message] || "설정을 불러오지 못했습니다.") + " (" + message + ")";
  }

  // text: an MCP address, a config code, or a config JSON (the part that
  // differs from the defaults).
  async function loadToken(text, announce) {
    setText("loadError", "");
    setText("loadStatus", "");
    const pasted = UI.parseConfigPaste(text, DEFAULTS);
    if (!pasted.ok) {
      setText("loadError", pasted.error);
      return false;
    }
    let body;
    if (pasted.kind === "json") {
      try {
        body = await postScenario({ action: "validate", config: pasted.patch });
      } catch (error) {
        body = { ok: true, config: UI.normalizeDerived(UI.applyPatch(DEFAULTS, pasted.patch)) };
      }
      if (!body.ok) {
        setText("loadError", UI.describeConfigError(body.error, SCHEMA).text);
        return false;
      }
    } else {
      try {
        body = await postScenario({ action: "decode", token: text });
      } catch (error) {
        // Without the server the config code can still be read here.
        try {
          body = { ok: true, config: UI.decodeToken(UI.tokenFromText(text) || text, DEFAULTS) };
        } catch (decodeError) {
          body = { ok: false, error: decodeError.message };
        }
      }
      if (!body.ok) {
        setText("loadError", loadErrorText(body.error));
        return false;
      }
    }
    loadConfig(body.config, body.token);
    if (announce) setText("loadStatus", "설정을 불러왔습니다.");
    return true;
  }

  function hashToken() {
    const match = /^#cfg=(.+)$/.exec(location.hash);
    if (!match) return "";
    try {
      return decodeURIComponent(match[1]);
    } catch (error) {
      return match[1];
    }
  }

  async function restoreFromHash() {
    const token = hashToken();
    if (!token || token === state.goodToken) return;
    state.restoring = true;
    try {
      await loadToken(token, false);
    } finally {
      state.restoring = false;
      render();
    }
  }

  function presetMatches(preset, query) {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    const haystack = (preset.label + "\n" + preset.description).toLowerCase();
    return words.every((word) => haystack.indexOf(word) >= 0);
  }

  // The search covers every option row and the error buttons. A group with
  // no match is hidden; one with a match is opened.
  function applySearch() {
    const query = byId("optionSearch").value.trim();
    let matches = 0;
    const sectionMatches = { build: 0, error: 0 };
    document.querySelectorAll("[data-group]").forEach((group) => {
      let groupMatches = 0;
      group.querySelectorAll(".opt").forEach((row) => {
        const match = UI.matchOptionSearch(OPTIONS[row.dataset.opt], query);
        row.hidden = !match;
        if (match) groupMatches += 1;
      });
      group.querySelectorAll("[data-detail]").forEach((box) => {
        const inside = Array.from(box.querySelectorAll(".opt")).filter((row) => !row.hidden).length;
        box.hidden = inside === 0;
        box.open = query ? inside > 0 : Boolean(state.groupChanged["detail:" + box.dataset.detail]);
      });
      matches += groupMatches;
      sectionMatches[GROUP_SECTION[group.dataset.group]] += groupMatches;
      group.hidden = groupMatches === 0;
      if (group.tagName === "DETAILS") {
        group.open = query
          ? groupMatches > 0
          : Boolean(state.groupChanged[group.dataset.group]) || group.dataset.defaultOpen === "1";
      }
    });
    let presetMatchCount = 0;
    document.querySelectorAll("#injectionCard [data-preset]").forEach((button) => {
      const match = !query || presetMatches(PRESETS[button.dataset.preset], query);
      button.hidden = !match;
      if (match) presetMatchCount += 1;
    });
    byId("injectionCard").hidden = presetMatchCount === 0;
    byId("buildPart").hidden = sectionMatches.build === 0;
    byId("errorPart").hidden = sectionMatches.error === 0 && presetMatchCount === 0;
    setText("searchCount", query ? "설정 " + matches + "개, 에러 버튼 " + presetMatchCount + "개" : "");
  }

  async function applyGlobal(payload, question, doneText) {
    if (!window.confirm(question)) return;
    setText("dangerStatus", "");
    let body;
    try {
      body = await postScenario(payload);
    } catch (error) {
      body = { ok: false, error: "요청을 보내지 못했습니다" };
    }
    if (!body.ok) {
      setText("dangerStatus", "실패: " + (body.error || "알 수 없는 오류"));
      return;
    }
    globalConfig = body.config;
    renderGlobal();
    setText("dangerStatus", doneText);
  }

  function resetGlobal() {
    return applyGlobal(
      { scenario: "ok" },
      "서버 기본값을 처음으로 되돌립니다. 주소에 설정이 없는 모든 요청에 적용됩니다. 계속할까요?",
      "서버 기본값을 되돌렸습니다."
    );
  }

  // Where the keyboard focus goes when the chip at index is removed: the chip
  // that takes its place, else the last chip, else the URL field.
  function focusChipAt(index) {
    const buttons = Array.from(byId("chips").querySelectorAll(".chip-remove"));
    const target = buttons[Math.min(index, buttons.length - 1)] || byId("mcpUrl");
    target.focus();
  }

  optionsCol.addEventListener("input", (event) => {
    if (event.target.id === "optionSearch") return;
    if (!event.target.closest(".opt")) return;
    syncFromForm();
    changed();
  });
  optionsCol.addEventListener("change", (event) => {
    if (!event.target.closest(".opt")) return;
    const control = event.target.closest("[data-config-path]");
    whenPointerIsUp(() => {
      syncFromForm();
      // Show what was understood (an emptied field falls back to the default).
      if (control && control.dataset.configKind !== "json") writeAdvanced(control, currentConfig());
      changed();
    });
  });
  document.addEventListener("click", (event) => {
    const target = event.target.closest("button");
    if (!target) return;
    if (target.dataset.reset) {
      const path = target.dataset.reset;
      if (target.classList.contains("chip-remove")) {
        const index = Array.from(byId("chips").querySelectorAll(".chip-remove")).indexOf(target);
        resetPath(path);
        focusChipAt(Math.max(index, 0));
      } else {
        resetPath(path);
        // The button hides with the change mark: stay on the option's control.
        const control = controlOf(path);
        if (control) control.focus();
      }
    } else if (target.dataset.preset) {
      applyPreset(PRESETS[target.dataset.preset]);
    } else if (target.dataset.copy) {
      handleCopy(target);
    } else if (target.dataset.expand) {
      byId(target.dataset.expand).classList.toggle("is-open");
      syncExpandButtons();
    }
  });
  byId("optionSearch").addEventListener("input", applySearch);
  byId("resetAll").addEventListener("click", () => {
    setText("presetDetail", "");
    loadConfig(UI.clone(DEFAULTS), "");
    // The button is disabled now: keep the focus in the result card.
    byId("mcpUrl").focus();
  });
  byId("urlErrorJump").addEventListener("click", () => {
    if (state.errorPath) revealOption(state.errorPath);
  });
  // "더 보기" and the compatibility box are <details>: the browser opens them
  // by mouse and by keyboard. Their copy texts get a height only then.
  ["moreBox", "compatBox"].forEach((id) => { byId(id).addEventListener("toggle", syncExpandButtons); });
  window.addEventListener("resize", syncExpandButtons);
  byId("loadTokenButton").addEventListener("click", () => loadToken(byId("loadToken").value, true));
  byId("loadToken").addEventListener("keydown", (event) => {
    // Enter loads a one-line address or code. A JSON keeps its line breaks.
    if (event.key === "Enter" && !event.shiftKey && byId("loadToken").value.trim()[0] !== "{") {
      event.preventDefault();
      loadToken(byId("loadToken").value, true);
    }
  });
  byId("loadToken").addEventListener("paste", () => {
    setTimeout(() => { if (byId("loadToken").value.trim()) loadToken(byId("loadToken").value, true); }, 0);
  });
  byId("applyGlobal").addEventListener("click", () => applyGlobal(
    { config: currentConfig() },
    "지금 설정을 서버 기본값으로 씁니다. 주소에 설정이 없는 모든 요청의 동작이 바뀝니다. 계속할까요?",
    "지금 설정을 서버 기본값으로 썼습니다."
  ));
  byId("resetGlobal").addEventListener("click", resetGlobal);
  byId("globalBannerReset").addEventListener("click", resetGlobal);
  byId("expireSessions").addEventListener("click", async () => {
    if (!window.confirm("지금까지 준 세션 ID를 모두 끊습니다. 계속할까요?")) return;
    let body;
    try {
      body = await postScenario({ action: "expireSessions" });
    } catch (error) {
      body = { ok: false, error: "요청을 보내지 못했습니다" };
    }
    setText("dangerStatus", body.ok ? "세션 " + body.expired + "개를 끊었습니다." : "실패: " + (body.error || "알 수 없는 오류"));
  });
  window.addEventListener("hashchange", restoreFromHash);

  writeForm(currentConfig());
  renderGlobal();
  if (hashToken()) {
    state.restoring = true;
    render();
    state.restoring = false;
    restoreFromHash();
  } else {
    render();
  }
})();
"""


# Methods each fixed route accepts, for the Allow header of a 405. /mcp depends on
# the config (Handler.mcp_allowed_methods).
# ---------------------------------------------------------------------------
# Screen metadata (GET /scenario/meta). Every option, group and preset the
# control pages show is defined here once, so the page and the API cannot drift.
# ---------------------------------------------------------------------------
# The screen has two parts. "build" holds what a server that follows the spec
# may be set to (the shape of a working server); "error" holds what breaks the
# spec or imitates a failure. Every option takes the section of its group.
CONFIG_OPTION_SECTIONS: tuple[dict[str, str], ...] = (
    {"id": "build", "label": "서버 만들기", "description": "정상 동작하는 MCP 서버의 모양을 정합니다"},
    {"id": "error", "label": "에러 넣기", "description": "일부러 실패하거나 스펙을 어기게 합니다"},
)

# ``open``: the group starts expanded on the screen (build groups are never folded).
CONFIG_OPTION_GROUPS: tuple[dict[str, Any], ...] = (
    {"id": "basic", "section": "build", "open": True, "label": "기본 정보", "description": "서버와 서비스의 이름"},
    {
        "id": "protocol", "section": "build", "open": True, "label": "지원 스펙",
        "description": "서버가 받는 MCP 요청의 종류",
    },
    {"id": "auth", "section": "build", "open": True, "label": "인증", "description": "토큰이 필요한 범위"},
    {"id": "tools", "section": "build", "open": True, "label": "툴", "description": "툴 목록의 모양"},
    {"id": "session", "section": "build", "open": True, "label": "세션", "description": "2025 방식의 세션 ID"},
    {
        "id": "errToolCall", "section": "error", "open": True, "label": "tools/call",
        "description": "툴 호출을 실패시키거나 늦춥니다",
    },
    {
        "id": "errToolsList", "section": "error", "open": True, "label": "tools/list",
        "description": "툴 목록을 실패시키거나 잘못된 목록을 줍니다",
    },
    {
        "id": "errAll", "section": "error", "open": False, "label": "모든 요청",
        "description": "모든 요청을 막거나 늦춥니다",
    },
    {
        "id": "errDiscover", "section": "error", "open": False, "label": "server/discover (2026-07-28)",
        "description": "서버 정보 응답을 실패시키거나 늦춥니다",
    },
    {
        "id": "errModernResponse", "section": "error", "open": False, "label": "응답 형식 깨기 (2026-07-28)",
        "description": "성공 응답에서 있어야 할 값을 빼거나 바꿉니다",
    },
    {
        "id": "errLegacyProbe", "section": "error", "open": False,
        "label": "2025 방식 서버가 2026-07-28 요청을 받았을 때",
        "description": "옛 서버가 새 요청에 답하는 방식을 고릅니다",
    },
    {
        "id": "errRaw", "section": "error", "open": False, "label": "직접 응답 지정",
        "description": "메서드별로 적어 둔 응답을 그대로 줍니다",
    },
)
CONFIG_GROUP_SECTIONS = {group["id"]: group["section"] for group in CONFIG_OPTION_GROUPS}
# DEFAULT_CONFIG leaves that config_option_schema() leaves out on purpose, with
# the reason. tests/test_scenario_meta.py checks that every other leaf is listed.
CONFIG_OPTION_EXCLUDED: dict[str, str] = {
    # Read by the /scenario page only (which request shape its preview sends).
    "protocolEra": "서버 동작에는 영향이 없는 값입니다(예전 화면이 쓰던 요청 방식 표시)",
    # normalize_config always rewrites it to (serverEra == "modern").
    "rejectLegacy": "serverEra=modern의 옛 이름입니다. 정규화하면 serverEra에서 계산됩니다",
    # normalize_config only reads customHeader.enabled; name and value stay fixed.
    "customHeader.name": "고정값(X-Mock-Auth)입니다. 설정으로 바꿀 수 없습니다",
    "customHeader.value": "고정값(allow)입니다. 설정으로 바꿀 수 없습니다",
}

# What the screens call the two sides of an exchange. The server side is the
# "지원 스펙" (serverEra), the request side the "요청 방식".
SERVER_ERA_LABELS = {"both": "둘 다", "legacy": "2025 방식만", "modern": "2026-07-28만"}
REQUEST_MODE_LABELS = {"legacy": "2025 방식", "modern": "2026 방식"}
SERVER_ERA_DESCRIPTIONS = {
    "both": "2025 방식과 2026-07-28 요청을 모두 받습니다 (기본)",
    "legacy": "initialize로 시작하는 2025 방식 요청만 압니다",
    "modern": "요청마다 버전을 싣는 2026-07-28 요청만 받습니다",
}
AUTH_MODE_LABELS = {"off": "없음", "oauth": "OAuth, 모든 요청", "mixed-oauth": "OAuth, 고른 툴만"}
AUTH_MODE_DESCRIPTIONS = {
    "off": "토큰 없이 씁니다 (기본)",
    "oauth": "모든 요청에 Bearer 토큰이 필요하고, 없으면 401을 줍니다",
    "mixed-oauth": "보호할 툴을 부를 때만 Bearer 토큰이 필요합니다",
}
SERVER_HTTP_STATUS_DESCRIPTIONS = {
    200: "막지 않음 (기본)",
    401: "401 Unauthorized로 답함",
    403: "403 Forbidden으로 답함",
}
SERVER_TARGET_DESCRIPTIONS = {
    "all": "모든 요청 (기본)",
    "initialize": "initialize 요청과 GET만",
}
CACHE_SCOPE_DESCRIPTIONS = {
    "private": "사용자별로만 캐시 (기본)",
    "public": "여럿이 함께 캐시",
}
INVALID_PARAMS_HTTP_STATUS_DESCRIPTIONS = {
    200: "HTTP 200 (기본)",
    400: "HTTP 400",
}
# JSON-RPC error codes this server emits, for display.
JSON_RPC_ERROR_CODES: tuple[tuple[int, str, str], ...] = (
    (-32700, "ParseError", "바디가 JSON으로 읽히지 않음"),
    (ERR_INVALID_REQUEST, "InvalidRequest", "JSON-RPC 요청 형태가 아님. 세션 에러와 Origin 거절에도 쓰입니다"),
    (ERR_METHOD_NOT_FOUND, "MethodNotFound", "모르는 메서드. 2026 방식에서는 HTTP 404와 함께 옵니다"),
    (ERR_INVALID_PARAMS, "InvalidParams", "잘못된 인자: 모르는 툴, 잘못된 커서, 필수 _meta 없음"),
    (ERR_INTERNAL, "InternalError", "서버 내부 오류. 넣은 에러의 기본 코드"),
    (ERR_HEADER_MISMATCH, "HeaderMismatch", "헤더와 바디가 다르거나 필수 헤더가 없음 (2026-07-28)"),
    (
        ERR_MISSING_CAPABILITY,
        "MissingRequiredClientCapability",
        "필요한 클라이언트 capability가 선언되지 않음 (2026-07-28)",
    ),
    (
        ERR_UNSUPPORTED_VERSION,
        "UnsupportedProtocolVersion",
        "서버가 지원하지 않는 프로토콜 버전 (2026-07-28)",
    ),
)

# python3: a bare "python" is missing on many systems.
CLIENT_COMMAND_PREFIX = "PYTHONPATH=src python3 -m unit_expert_mcp.client"


def _choices(descriptions: dict[Any, str], labels: dict[Any, str] | None = None) -> list[dict[str, Any]]:
    """Enum choices. ``labels`` gives a plain name to show next to the value."""
    choices: list[dict[str, Any]] = []
    for value, text in descriptions.items():
        choice: dict[str, Any] = {"value": value, "description": text}
        if labels and value in labels:
            choice["label"] = labels[value]
        choices.append(choice)
    return choices


def config_default_at(path: str) -> Any:
    """DEFAULT_CONFIG value at a dotted path (a copy)."""
    value: Any = DEFAULT_CONFIG
    for key in path.split("."):
        value = value[key]
    return json.loads(json.dumps(value))


def _option(
    path: str,
    group: str,
    kind: str,
    label: str,
    description: str,
    applies_to: str = "both",
    *,
    choices: list[dict[str, Any]] | None = None,
    low: float | None = None,
    high: float | None = None,
    depends_on: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "path": path,
        "label": label,
        "description": description,
        "kind": kind,
        "choices": choices,
        "min": low,
        "max": high,
        "group": group,
        "section": CONFIG_GROUP_SECTIONS[group],
        "appliesTo": applies_to,
        "dependsOn": depends_on,
        "default": config_default_at(path),
        **extra,
    }


def _when(path: str, *values: Any) -> dict[str, Any]:
    """dependsOn condition: the option only has an effect while path is one of values."""
    return {"path": path, "in": list(values)}


def config_option_schema() -> list[dict[str, Any]]:
    """Every option a control page can set, in display order.

    kind: bool | enum | int | float | string | list | json. ``choices`` lists the
    allowed values of an enum (and of a list whose items come from a fixed set).
    ``section`` is "build" (a setting a server that follows the spec may have)
    or "error" (breaks the spec or imitates a failure); it comes from the group.
    ``appliesTo`` matches the "적용 era" column of docs/CONFIG_REFERENCE.md.
    ``dependsOn`` is a condition on another option ("in": allowed values, or
    "min": lowest value) without which this option has no effect.
    ``strictOnly`` marks options that are ignored when strict2026 is off.
    ``detail`` marks a rarely used option the screen keeps under "세부 설정".
    """
    delay_max = MAX_INJECTED_DELAY_SECONDS
    strict = {"strictOnly": True}
    detail = {"detail": True}
    return [
        # == build: the shape of a working server ============================
        # -- basic ----------------------------------------------------------
        _option(
            "mcp.identifier", "basic", "string", "서버 이름",
            "서버가 자기를 소개할 때 쓰는 이름입니다",
            maxLength=128,
        ),
        _option(
            "mcp.serviceName", "basic", "string", "서비스 이름",
            "툴 설명에 들어가는 서비스 이름입니다",
            maxLength=200,
        ),
        # -- protocol -------------------------------------------------------
        _option(
            "serverEra", "protocol", "enum", "지원 스펙",
            "서버가 어떤 MCP 요청을 받을지 고릅니다",
            choices=_choices(
                {era: SERVER_ERA_DESCRIPTIONS[era] for era in SERVER_ERAS}, SERVER_ERA_LABELS
            ),
        ),
        _option(
            "strict2026", "protocol", "bool", "엄격 검증",
            "2026-07-28 요청을 스펙대로 꼼꼼히 검사합니다",
            "modern",
        ),
        _option(
            "initialize.protocolVersionEnabled", "protocol", "bool", "initialize 응답 버전 고정",
            "켜면 아래 버전으로 답하고, 끄면 클라이언트가 요청한 버전으로 답합니다",
            "legacy",
        ),
        _option(
            "initialize.protocolVersion", "protocol", "string", "initialize 응답 버전",
            "initialize 응답에 넣는 protocolVersion 값입니다",
            "legacy",
            depends_on=_when("initialize.protocolVersionEnabled", True),
            suggestions=[
                {"value": version, "description": text} for version, text in PROTOCOL_VERSION_CHOICES
            ],
        ),
        _option(
            "modern.requiredClientCapabilities", "protocol", "list", "클라이언트가 선언해야 하는 기능",
            "요청이 이 기능을 선언하지 않으면 400(-32021)으로 거절합니다",
            "modern",
            choices=[{"value": name, "description": name} for name in CLIENT_CAPABILITY_NAMES],
            **strict, **detail,
        ),
        _option(
            "modern.invalidParamsHttpStatus", "protocol", "enum", "잘못된 인자(-32602)의 HTTP 상태",
            "모르는 툴이나 잘못된 커서에 답할 때의 HTTP 상태입니다",
            "modern", choices=_choices(INVALID_PARAMS_HTTP_STATUS_DESCRIPTIONS), **strict, **detail,
        ),
        _option(
            "modern.rejectUnexpectedParamHeader", "protocol", "bool", "남는 Mcp-Param 헤더 거절",
            "바디에 없는 값을 가리키는 Mcp-Param-* 헤더가 오면 400(-32020)으로 거절합니다",
            "modern", **strict, **detail,
        ),
        # -- auth -----------------------------------------------------------
        _option(
            "auth.mode", "auth", "enum", "인증 방식",
            "Bearer 토큰을 어디까지 요구할지 고릅니다",
            choices=_choices({mode: AUTH_MODE_DESCRIPTIONS[mode] for mode in AUTH_MODES}, AUTH_MODE_LABELS),
        ),
        _option(
            "auth.protectedTools", "auth", "list", "보호할 툴",
            "토큰이 있어야 부를 수 있는 툴 이름입니다",
            depends_on=_when("auth.mode", "mixed-oauth"),
        ),
        _option(
            "auth.enforceScope", "auth", "bool", "스코프 검사",
            "토큰에 mcp 스코프가 없으면 403을 줍니다",
            depends_on=_when("auth.mode", "oauth", "mixed-oauth"),
        ),
        _option(
            "allowedOrigins", "auth", "list", "허용할 Origin",
            "적어 두면 목록에 없는 Origin의 요청을 403으로 막습니다",
            maxItems=50, **detail,
        ),
        # -- tools ----------------------------------------------------------
        _option(
            "modern.testTools", "tools", "bool", "테스트 툴 보이기",
            f"툴 목록에 테스트용 툴({HEADER_PARAMS_TOOL}, {MRTR_TOOL})을 더합니다",
            "modern", **strict,
        ),
        _option(
            "toolsList.pageSize", "tools", "int", "한 페이지의 툴 수",
            "0이면 한 번에 다 주고, 1 이상이면 그 수만큼 나눠 줍니다",
            low=0, high=100,
        ),
        _option(
            "toolsList.cacheTtlMs", "tools", "int", "툴 목록 캐시 시간(ms)",
            "클라이언트가 툴 목록을 다시 묻지 않아도 되는 시간입니다",
            "modern", low=0, high=3_600_000,
        ),
        _option(
            "toolsList.cacheScope", "tools", "enum", "툴 목록 캐시 범위",
            "툴 목록 캐시를 누구와 함께 쓸지 정합니다",
            "modern", choices=_choices(CACHE_SCOPE_DESCRIPTIONS),
        ),
        _option(
            "discover.cacheTtlMs", "tools", "int", "서버 정보 캐시 시간(ms)",
            "클라이언트가 server/discover를 다시 묻지 않아도 되는 시간입니다",
            "modern", low=0, high=86_400_000, **strict, **detail,
        ),
        _option(
            "discover.cacheScope", "tools", "enum", "서버 정보 캐시 범위",
            "server/discover 캐시를 누구와 함께 쓸지 정합니다",
            "modern", choices=_choices(CACHE_SCOPE_DESCRIPTIONS), **strict, **detail,
        ),
        # -- session --------------------------------------------------------
        _option(
            "session.issueId", "session", "bool", "세션 ID 주기",
            "initialize 응답에 Mcp-Session-Id를 붙입니다",
            "legacy",
        ),
        _option(
            "session.validate", "session", "bool", "세션 검사",
            "세션 ID가 없는 요청에 400, 모르는 세션에 404를 줍니다",
            "legacy", depends_on=_when("session.issueId", True),
        ),
        # == error: break the spec or imitate a failure ======================
        # -- tools/call -----------------------------------------------------
        _option(
            "toolCall.mode", "errToolCall", "enum", "tools/call 응답",
            "툴 호출에 줄 응답을 고릅니다",
            choices=_choices(TOOL_CALL_MODES),
        ),
        _option(
            "toolCall.errorCode", "errToolCall", "int", "tools/call 에러 코드",
            "JSON-RPC 에러로 답할 때 쓰는 코드입니다",
            low=-32768, high=32767,
            depends_on=_when("toolCall.mode", "json-rpc-error"),
        ),
        _option(
            "toolCall.httpStatus", "errToolCall", "int", "tools/call HTTP 상태",
            "HTTP 에러로 답할 때 쓰는 상태 코드입니다",
            low=400, high=599,
            depends_on=_when("toolCall.mode", "http-status"),
        ),
        _option(
            "toolCall.sseNotificationCount", "errToolCall", "int", "tools/call SSE 알림 수",
            "SSE로 답할 때 최종 응답 앞에 보내는 알림 수입니다",
            low=0, high=20,
            depends_on=_when("toolCall.mode", "sse"),
        ),
        _option(
            "toolCall.delaySeconds", "errToolCall", "float", "tools/call 늦게 답하기(초)",
            "툴 호출에 이 시간만큼 늦게 답합니다",
            low=0, high=delay_max,
        ),
        _option(
            "toolCall.targetTools", "errToolCall", "list", "tools/call 적용할 툴",
            "적은 툴에만 적용하고, 비우면 모든 툴에 적용합니다",
            maxItems=50,
        ),
        # -- tools/list -----------------------------------------------------
        _option(
            "toolsList.mode", "errToolsList", "enum", "tools/list 응답",
            "툴 목록 요청에 줄 응답을 고릅니다",
            choices=_choices(TOOLS_LIST_MODES),
        ),
        _option(
            "toolsList.tooManyCount", "errToolsList", "int", "툴 개수",
            "툴을 많이 줄 때의 개수입니다",
            low=1, high=100,
            depends_on=_when("toolsList.mode", "too-many"),
        ),
        _option(
            "toolsList.duplicateToolName", "errToolsList", "bool", "같은 이름의 툴 넣기",
            "이름이 같은 툴 두 개를 목록에 넣습니다",
            depends_on=_when("toolsList.mode", "normal", "too-many"),
        ),
        _option(
            "toolErrors", "errToolsList", "list", "잘못된 툴 정의",
            "고른 문제를 가진 툴로 목록을 바꿉니다",
            choices=[
                {
                    "value": name,
                    "label": SCENARIO_TITLES[name],
                    "description": SCENARIO_DESCRIPTIONS[name],
                }
                for name in TOOL_ERROR_SCENARIOS
            ],
            depends_on=_when("toolsList.mode", "normal"),
        ),
        _option(
            "toolsList.failAtPage", "errToolsList", "int", "실패할 페이지",
            "나눠 줄 때 이 번째 페이지 요청에 에러를 줍니다(0이면 끔)",
            low=0, high=100,
            depends_on={"path": "toolsList.pageSize", "min": 1},
        ),
        # -- every request --------------------------------------------------
        _option(
            "server.httpStatus", "errAll", "enum", "요청 막기",
            "요청을 처리하지 않고 401이나 403으로 답합니다",
            choices=_choices(SERVER_HTTP_STATUS_DESCRIPTIONS),
        ),
        _option(
            "server.target", "errAll", "enum", "막을 요청",
            "어떤 요청을 막을지 고릅니다",
            choices=_choices(SERVER_TARGET_DESCRIPTIONS),
            depends_on=_when("server.httpStatus", 401, 403),
        ),
        _option(
            "customHeader.enabled", "errAll", "bool", "X-Mock-Auth 헤더 검사",
            "X-Mock-Auth 헤더가 없으면 401, 값이 allow가 아니면 403을 줍니다",
        ),
        _option(
            "server.delayEnabled", "errAll", "bool", "모든 응답 늦추기",
            "모든 요청에 아래 시간만큼 늦게 답합니다",
        ),
        _option(
            "server.delaySeconds", "errAll", "float", "늦출 시간(초)",
            "모든 응답을 늦추는 시간입니다",
            low=0, high=30,
            depends_on=_when("server.delayEnabled", True),
        ),
        # -- server/discover (2026) -----------------------------------------
        _option(
            "discover.mode", "errDiscover", "enum", "server/discover 응답",
            "서버 정보 요청에 줄 응답을 고릅니다",
            "modern", choices=_choices(DISCOVER_MODES), **strict,
        ),
        _option(
            "discover.errorCode", "errDiscover", "int", "discover 에러 코드",
            "JSON-RPC 에러로 답할 때 쓰는 코드입니다",
            "modern", low=-32768, high=32767,
            depends_on=_when("discover.mode", "json-rpc-error"), **strict,
        ),
        _option(
            "discover.httpStatus", "errDiscover", "int", "discover HTTP 상태",
            "HTTP 5xx로 답할 때 쓰는 상태 코드입니다",
            "modern", low=500, high=599,
            depends_on=_when("discover.mode", "http-5xx"), **strict,
        ),
        _option(
            "discover.delaySeconds", "errDiscover", "float", "discover 늦게 답하기(초)",
            "서버 정보 요청에 이 시간만큼 늦게 답합니다",
            "modern", low=0, high=delay_max, **strict,
        ),
        # -- broken response shape (2026) -----------------------------------
        _option(
            "modernResponse.resultType", "errModernResponse", "enum", "resultType 바꾸기",
            "성공 응답의 resultType을 빼거나 다른 값으로 바꿉니다",
            "modern", choices=_choices(MODERN_RESULT_TYPE_MODES), **strict,
        ),
        _option(
            "modernResponse.omitServerInfo", "errModernResponse", "bool", "serverInfo 빼기",
            "응답의 _meta에서 serverInfo를 뺍니다",
            "modern", **strict,
        ),
        _option(
            "modernResponse.omitCacheFields", "errModernResponse", "bool", "캐시 값 빼기",
            "응답에서 ttlMs와 cacheScope를 뺍니다",
            "modern", **strict,
        ),
        _option(
            "modernResponse.issueSessionId", "errModernResponse", "bool", "세션 ID 붙이기",
            "2026-07-28 응답에 없어야 할 Mcp-Session-Id 헤더를 붙입니다",
            "modern", **strict,
        ),
        _option(
            "modernResponse.methods", "errModernResponse", "list", "형식을 깰 메서드",
            "고른 메서드의 응답만 바꾸고, 비우면 모두 바꿉니다",
            "modern",
            choices=[{"value": name, "description": name} for name in MODERN_RESULT_METHODS],
            **strict,
        ),
        # -- a 2025-only server that gets a 2026 request --------------------
        _option(
            "legacyModernProbe.mode", "errLegacyProbe", "enum", "2026-07-28 요청에 줄 응답",
            "2025 방식만 아는 서버가 2026-07-28 요청에 답하는 방식을 고릅니다",
            "legacy", choices=_choices(LEGACY_MODERN_PROBE_MODES),
            depends_on=_when("serverEra", "legacy"),
        ),
        _option(
            "legacyModernProbe.delaySeconds", "errLegacyProbe", "float", "답하지 않을 시간(초)",
            "응답 없이 버티는 시간입니다",
            "legacy", low=0, high=delay_max,
            depends_on=_when("legacyModernProbe.mode", "timeout"),
        ),
        # -- raw responses --------------------------------------------------
        _option(
            "rawResponses", "errRaw", "json", "직접 지정한 응답",
            "메서드별로 적어 둔 응답을 다른 설정보다 먼저 그대로 줍니다",
            maxItems=RAW_RESPONSE_MAX_RULES,
            itemFields=["enabled", "method", "status", "headers", "contentType", "body"],
        ),
    ]


def preset_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Normalized full config of a preset: its patch merged onto the defaults."""
    config = default_config()
    _merge_config_patch(config, patch)
    return normalize_config(config)


def _patch_paths(patch: dict[str, Any], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, value in patch.items():
        if isinstance(value, dict):
            paths.extend(_patch_paths(value, f"{prefix}{key}."))
        else:
            paths.append(f"{prefix}{key}")
    return paths


def scenario_presets() -> list[dict[str, Any]]:
    """Ready-made partial configs. category: "matrix" | "injection".

    Matrix presets are the four client/server pairs of the README section
    "게이트웨이 테스트 매트릭스"; ``cli`` is the client command with ``{url}``
    standing for the MCP endpoint the client connects to. ``section`` of an
    injection preset is "error" when it sets any error option, else "build".
    """
    sections = {option["path"]: option["section"] for option in config_option_schema()}

    def matrix(preset_id: str, server_era: str, client_mode: str, groups: str, expect: str) -> dict[str, Any]:
        mode = REQUEST_MODE_LABELS[client_mode]
        server = SERVER_ERA_LABELS[server_era]
        return {
            "id": preset_id,
            "label": f"{mode} 요청 → {server} 지원하는 서버",
            "description": f"{mode}으로 요청하는 클라이언트가 {server} 지원하는 서버에 붙는 조합입니다",
            "category": "matrix",
            "section": "build",
            "patch": {"serverEra": server_era},
            "clientMode": client_mode,
            "expect": expect,
            "cli": f"{CLIENT_COMMAND_PREFIX} run {groups} --url {{url}} --expect gateway",
        }

    def injection(preset_id: str, label: str, description: str, patch: dict[str, Any]) -> dict[str, Any]:
        is_error = any(sections.get(path) == "error" for path in _patch_paths(patch))
        return {
            "id": preset_id,
            "label": label,
            "description": description,
            "category": "injection",
            "section": "error" if is_error else "build",
            "patch": patch,
        }

    return [
        matrix(
            "matrix-modern-modern", "modern", "modern",
            "modern modern-violation compat-modern-client",
            "스펙대로면 동작합니다",
        ),
        matrix(
            "matrix-modern-legacy", "legacy", "modern",
            "modern compat-modern-client",
            "서버에 직접 붙으면 실패합니다. 중간에서 2025 방식으로 바꿔 주면 동작합니다",
        ),
        matrix(
            "matrix-legacy-legacy", "legacy", "legacy",
            "legacy compat-legacy-client",
            "동작합니다",
        ),
        matrix(
            "matrix-legacy-modern", "modern", "legacy",
            "legacy compat-legacy-client",
            "실패해야 합니다. 서버에 직접 붙으면 initialize가 400(-32022)을 받습니다",
        ),
        injection(
            "auth-oauth-401", "OAuth 인증 켜기",
            "토큰 없는 요청에 401을 줍니다",
            {"auth": {"mode": "oauth"}},
        ),
        injection(
            "response-delay", "모든 응답 5초 늦추기",
            "모든 요청에 5초 늦게 답합니다",
            {"server": {"delayEnabled": True, "delaySeconds": DEFAULT_DELAY_SECONDS}},
        ),
        injection(
            "tools-list-error", "tools/list 에러",
            "툴 목록 요청에 JSON-RPC 에러(-32603)를 줍니다",
            {"toolsList": {"mode": "json-rpc-error"}},
        ),
        injection(
            "tools-list-empty", "tools/list 빈 목록",
            "툴이 하나도 없는 목록을 줍니다",
            {"toolsList": {"mode": "empty"}},
        ),
        injection(
            "tools-list-paged", "툴 목록 2개씩 나눠 주기",
            "한 페이지에 툴 2개씩 줍니다",
            {"toolsList": {"pageSize": 2}},
        ),
        injection(
            "tool-call-is-error", "tools/call isError",
            "툴 호출에 isError: true 결과를 줍니다",
            {"toolCall": {"mode": "is-error"}},
        ),
        injection(
            "tool-call-json-rpc-error", "tools/call JSON-RPC 에러",
            "툴 호출에 JSON-RPC 에러(-32603)를 줍니다",
            {"toolCall": {"mode": "json-rpc-error"}},
        ),
        injection(
            "tool-call-http-500", "tools/call HTTP 500",
            "툴 호출에 HTTP 500으로 답합니다",
            {"toolCall": {"mode": "http-status", "httpStatus": 500}},
        ),
        injection(
            "tool-call-disconnect", "tools/call 연결 끊기",
            "툴 호출에 답하지 않고 연결을 끊습니다",
            {"toolCall": {"mode": "disconnect"}},
        ),
        injection(
            "tool-call-sse", "tools/call SSE",
            "툴 호출에 SSE로 알림 2건을 보낸 뒤 답합니다",
            {"toolCall": {"mode": "sse"}},
        ),
        injection(
            "discover-no-modern-version", "discover에 2026-07-28 없음",
            "server/discover가 2025 버전만 지원한다고 답합니다",
            {"discover": {"mode": "no-modern-version"}},
        ),
        injection(
            "discover-http-5xx", "discover HTTP 503",
            "server/discover에 HTTP 503으로 답합니다",
            {"discover": {"mode": "http-5xx", "httpStatus": 503}},
        ),
        injection(
            "legacy-probe-http-400", "2025 방식 서버가 2026-07-28 요청에 400",
            "2025 방식만 아는 서버가 2026-07-28 요청에 HTTP 400(-32600)을 줍니다",
            {"serverEra": "legacy", "legacyModernProbe": {"mode": "http-400"}},
        ),
        injection(
            "session-validate", "세션 검사 켜기",
            "세션 ID가 없는 요청에 400, 모르는 세션에 404를 줍니다",
            {"session": {"validate": True}},
        ),
    ]
def scenario_header_names() -> list[dict[str, Any]]:
    """Values X-MCP-Test-Scenario accepts.

    kind: "scenario" (also accepted by POST /scenario {"scenario": ...}),
    "header-only", or "pattern" (the name has a variable part). ``patch`` is the
    partial config the name stands for.
    """

    def entry(name: str, kind: str, description: str, title: str | None = None) -> dict[str, Any]:
        return {
            "name": name,
            "kind": kind,
            "title": title,
            "description": description,
            "patch": compact_config(scenario_to_config(name)),
        }

    def setting(patch: dict[str, Any], prefix: str = "") -> str:
        parts: list[str] = []
        for key, value in patch.items():
            if isinstance(value, dict):
                parts.append(setting(value, f"{prefix}{key}."))
            else:
                parts.append(f"{prefix}{key}={json.dumps(value, ensure_ascii=False)}")
        return ", ".join(parts)

    names = [
        entry(name, "scenario", description, SCENARIO_TITLES.get(name))
        for name, description in SCENARIO_DESCRIPTIONS.items()
    ]
    for name, era in SERVER_ERA_SCENARIOS.items():
        names.append(entry(name, "header-only", f"serverEra={json.dumps(era)}"))
    for name, patch in header_scenario_patches().items():
        names.append(entry(name, "header-only", setting(patch)))
    names.append(
        {
            "name": f"{HEADER_SCENARIO_INITIALIZE_VERSION_PREFIX}<버전>",
            "kind": "pattern",
            "title": None,
            "description": "initialize.protocolVersion을 그 버전으로 고정합니다",
            "patch": None,
        }
    )
    names.append(
        {
            "name": f"{HEADER_SCENARIO_CONFIG_PREFIX}<토큰>",
            "kind": "pattern",
            "title": None,
            "description": "설정 코드에 담긴 설정 전체입니다. 쉼표로 다른 이름과 이을 수 없습니다",
            "patch": None,
        }
    )
    return names


def scenario_meta() -> dict[str, Any]:
    """Everything a control page needs to render the config form (GET /scenario/meta)."""
    return {
        "defaults": default_config(),
        "sections": [dict(section) for section in CONFIG_OPTION_SECTIONS],
        "groups": [dict(group) for group in CONFIG_OPTION_GROUPS],
        "options": config_option_schema(),
        "excluded": dict(CONFIG_OPTION_EXCLUDED),
        "presets": scenario_presets(),
        "scenarioHeader": TEST_SCENARIO_HEADER,
        "scenarioHeaders": scenario_header_names(),
        "errorCodes": [
            {"code": code, "name": name, "description": description}
            for code, name, description in JSON_RPC_ERROR_CODES
        ],
    }


def describe_config(config: dict[str, Any]) -> dict[str, Any]:
    """A normalized config with its compact form and cfg token."""
    return {"config": config, "compact": compact_config(config), "token": encode_config_token(config)}


def config_token_from_text(raw_text: Any) -> str:
    """The cfg token inside a pasted value: a bare token, ``cfg:<token>``,
    ``cfg=<token>``, a URL whose query carries ``cfg`` or the whole header line
    ``X-MCP-Test-Scenario: cfg:<token>``."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("token must be a non-empty string")
    text = raw_text.strip()
    header_prefix = TEST_SCENARIO_HEADER.lower() + ":"
    if text.lower().startswith(header_prefix):
        text = text[len(header_prefix):].strip()
    if text.lower().startswith(HEADER_SCENARIO_CONFIG_PREFIX):
        return text[len(HEADER_SCENARIO_CONFIG_PREFIX):]
    if "?" in text or "://" in text or text.startswith("/"):
        query = urlparse(text).query
    elif "=" in text.rstrip("="):
        query = text
    else:
        return text
    token = parse_qs(query).get(CONFIG_QUERY_PARAM, [""])[0]
    if not token:
        raise ValueError(f"no {CONFIG_QUERY_PARAM} parameter in the URL")
    return token


def scenario_action(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a POST /scenario action. Raises ValueError for a bad request.

    validate and decode only compute; expireSessions forgets sessions. None of
    them changes the active config.
    """
    action = payload["action"]
    if action == "expireSessions":
        return {"ok": True, "action": action, "expired": expire_sessions()}
    if action == "validate":
        raw_config = payload.get("config")
        if not isinstance(raw_config, dict):
            raise ValueError("config must be an object")
        return {"ok": True, "action": action, **describe_config(normalize_config(raw_config))}
    if action == "decode":
        token = config_token_from_text(payload.get("token"))
        return {"ok": True, "action": action, **describe_config(decode_config_token(token))}
    raise ValueError(f"unknown action '{action}'")


ROUTE_METHODS = {
    "/": "GET, OPTIONS",
    "/scenario": "GET, POST, OPTIONS",
    "/healthz": "GET, OPTIONS",
    "/favicon.ico": "GET, OPTIONS",
    "/inspect": "GET, OPTIONS",
    "/send": "GET, OPTIONS",
    "/inspect/log": "GET, OPTIONS",
    "/scenario/meta": "GET, OPTIONS",
    "/.well-known/oauth-protected-resource": "GET, OPTIONS",
    "/.well-known/oauth-authorization-server": "GET, OPTIONS",
    "/authorize": "GET, POST, OPTIONS",
    "/register": "POST, OPTIONS",
    "/token": "POST, OPTIONS",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UnitExpertMCP/1.0"

    def handle_one_request(self) -> None:
        # Per-request inspection state: when the request arrived, the entries it
        # recorded and the status it answered with.
        self._inspect_started: float | None = None
        self._inspect_entries: list[dict[str, Any]] = []
        self._inspect_status: int | None = None
        self._inspect_delay: list[str] = []
        try:
            super().handle_one_request()
        finally:
            self.finish_inspection()

    def parse_request(self) -> bool:
        self._inspect_started = time.monotonic()
        self._inspect_entries = []
        self._inspect_status = None
        self._inspect_delay = []
        return super().parse_request()

    def send_response(self, code: int, message: str | None = None) -> None:
        self._inspect_status = int(code)
        super().send_response(code, message)

    def finish_inspection(self) -> None:
        """After the response is written: fill durationMs, and log a /mcp
        response that no branch recorded so the inspection log has no gaps."""
        started = self._inspect_started
        if started is None:
            return
        raw_path = getattr(self, "path", None)
        if (
            not self._inspect_entries
            and self._inspect_status is not None
            and isinstance(raw_path, str)
            and urlparse(raw_path).path == "/mcp"
            and getattr(self, "headers", None) is not None
        ):
            self.record(str(getattr(self, "command", "")), "/mcp", None, self._inspect_status, None)
        finish_exchanges(self._inspect_entries, started)
        self._inspect_started = None

    def config_source(self) -> str:
        """Where request_config() takes this request's config from."""
        query = parse_qs(urlparse(self.path).query)
        if query.get(CONFIG_QUERY_PARAM, [""])[0]:
            return "url"
        header = self.headers.get(TEST_SCENARIO_HEADER)
        if header is not None and header.strip():
            return "header"
        return "global"

    def record(
        self,
        http_method: str,
        path: str,
        request_body: Any,
        status: int,
        response_body: Any,
        **details: Any,
    ) -> None:
        """record_exchange for this request, with its URL query, config source and timing.

        A server.delaySeconds wait this request already sat through is named
        in ``injected`` after the options the caller passes.
        """
        if self._inspect_delay:
            details["injected"] = list(details.get("injected") or []) + self._inspect_delay
        entry = record_exchange(
            http_method,
            path,
            self.headers,
            request_body,
            status,
            response_body,
            query=urlparse(self.path).query,
            config_source=self.config_source(),
            started=self._inspect_started,
            **details,
        )
        self._inspect_entries.append(entry)

    def record_text(
        self,
        http_method: str,
        path: str,
        request_body: Any,
        status: int,
        text: str,
        config: dict[str, Any] | None = None,
        injected: list[str] | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        """Log a plain-text /mcp answer (the caller sends it)."""
        self.record(
            http_method,
            path,
            request_body,
            status,
            {"error": text},
            server_era=config_server_era(config) if config is not None else None,
            injected=injected,
            response_headers=response_headers,
        )

    def record_405(self, http_method: str, path: str, config: dict[str, Any] | None) -> None:
        """Log send_405()'s default answer (Allow: POST, OPTIONS)."""
        self.record_text(
            http_method, path, None, 405, "Method Not Allowed", config,
            response_headers={"Allow": "POST, OPTIONS"},
        )

    def record_auth_failure(
        self,
        http_method: str,
        path: str,
        request_body: Any,
        status: int,
        config: dict[str, Any],
    ) -> None:
        """Log the 401/403 that enforce_auth() already sent."""
        injected = [f"auth.mode={config['auth']['mode']}"]
        if status == 403:
            injected.append("auth.enforceScope")
        self.record(
            http_method,
            path,
            request_body,
            status,
            {"error": "invalid_token" if status == 401 else "insufficient_scope"},
            server_era=config_server_era(config),
            injected=injected,
        )

    def do_OPTIONS(self) -> None:
        if urlparse(self.path).path == "/mcp":
            self.record("OPTIONS", "/mcp", None, int(HTTPStatus.NO_CONTENT), None)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.end_headers()

    def __getattr__(self, name: str) -> Any:
        # BaseHTTPRequestHandler answers a method without a do_<METHOD> handler
        # (PUT, PATCH, HEAD, ...) with 501. Those are known methods this resource
        # does not allow, so answer 405 + Allow instead (RFC 9110 15.5.6).
        if name.startswith("do_"):
            return self.handle_unsupported_method
        raise AttributeError(name)

    def handle_unsupported_method(self) -> None:
        path = urlparse(self.path).path
        if path != "/mcp":
            allow = ROUTE_METHODS.get(path)
            if allow is None:
                self.send_text(404, "Not found")
            else:
                self.send_405(allow)
            self.close_connection = True
            return
        try:
            config = self.request_config()
        except ValueError:
            config = None
        allow = self.mcp_allowed_methods(config)
        self.record_text(
            self.command, path, None, 405, "Method Not Allowed", config,
            response_headers={"Allow": allow},
        )
        self.send_405(allow)
        # The request body (if any) was not read, so the connection cannot be reused.
        self.close_connection = True

    def mcp_allowed_methods(self, config: dict[str, Any] | None) -> str:
        """Allow header value for /mcp under the given config."""
        era = config_server_era(config)
        methods = ["POST", "OPTIONS"]
        if not (era == "modern" and (config is None or config["strict2026"])):
            methods.insert(0, "GET")
        if (
            config is not None
            and era != "modern"
            and config["session"]["validate"]
            and config["session"]["issueId"]
        ):
            methods.append("DELETE")
        return ", ".join(methods)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self.send_json(200, {"ok": True, "service": SERVER_NAME})
            return

        if path in {"/", "/scenario"}:
            self.send_html(200, render_scenario_page())
            return

        if path == "/favicon.ico":
            # The control pages have no icon; an empty answer keeps the browser
            # console free of a 404.
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_cors_headers()
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            return

        if path == "/inspect":
            self.send_html(200, render_inspect_page())
            return

        if path == "/inspect/log":
            raw_query = urlparse(self.path).query
            try:
                exchanges = inspect_query(raw_query)
            except ValueError as error:
                self.send_json(400, {"ok": False, "error": str(error)})
                return
            log_body: dict[str, Any] = {"exchanges": exchanges}
            if inspect_groups_requested(raw_query):
                log_body["groups"] = inspect_groups()
            self.send_json(200, log_body)
            return

        if path == "/send":
            self.send_html(200, render_send_page())
            return

        if path == "/scenario/meta":
            self.send_json(200, scenario_meta())
            return

        if path == "/.well-known/oauth-protected-resource":
            self.send_json(200, protected_resource_metadata(self.base_url()))
            return

        if path == "/.well-known/oauth-authorization-server":
            self.send_json(200, authorization_server_metadata(self.base_url()))
            return

        if path == "/authorize":
            self.handle_authorize_get()
            return

        if path != "/mcp":
            self.send_text(404, "Not found")
            return

        # 2026-07-28 removed the GET stream endpoint (stateless). A 2026 client
        # (detected via MCP-Protocol-Version) gets 405; legacy keeps the stream.
        # A legacy-only server (serverEra="legacy") does not know that rule, so
        # the request falls through to the legacy GET handling below.
        # A modern-only server (serverEra="modern", strict2026) answers 405 to
        # every GET, markers or not (spec: Backward Compatibility, SHOULD).
        try:
            early_config = self.request_config()
        except ValueError:
            early_config = None
        if early_config is not None and self.reject_origin(early_config, "GET", path):
            return
        early_era = config_server_era(early_config)
        if early_era != "legacy" and (
            request_era(None, self.headers) == "2026"
            or (early_era == "modern" and early_config is not None and early_config["strict2026"])
        ):
            self.record_405("GET", path, early_config)
            self.send_405()
            return

        try:
            config = self.request_config()
        except ValueError as error:
            self.record_text("GET", path, None, 400, str(error))
            self.send_text(400, str(error))
            return
        if self.handle_pre_json_rpc_config(config):
            return
        auth_status = self.enforce_auth(config, None, None)
        if auth_status:
            self.record_auth_failure("GET", path, None, auth_status, config)
            return

        accept = self.headers.get("Accept", "")
        if "text/event-stream" not in accept:
            text = "Invalid Accept header. Expected TEXT_EVENT_STREAM"
            self.record_text("GET", path, None, 400, text, config)
            self.send_text(400, text)
            return

        session_id = self.headers.get("mcp-session-id")
        if config["session"]["validate"] and config["session"]["issueId"]:
            failure = session_error(session_id)
            if failure is not None:
                self.record(
                    "GET",
                    path,
                    None,
                    failure[0],
                    failure[1],
                    server_era=config_server_era(config),
                    injected=["session.validate"],
                )
                self.send_json(failure[0], failure[1])
                return
        if not session_id:
            text = "Session ID required in mcp-session-id header"
            self.record_text("GET", path, None, 400, text, config)
            self.send_text(400, text)
            return

        self.record(
            "GET", path, None, 200, {"text": ": connected"}, server_era=config_server_era(config)
        )
        self.send_response(HTTPStatus.OK)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b": connected\n\n")

    def do_DELETE(self) -> None:
        # 2026-07-28 is stateless: there is no session to terminate, so DELETE
        # /mcp is not allowed. Legacy also has no DELETE handling here.
        # With session.validate a legacy client may end its session with DELETE.
        path = urlparse(self.path).path
        if path != "/mcp":
            self.send_text(404, "Not found")
            return
        try:
            config = self.request_config()
        except ValueError:
            self.record_405("DELETE", path, None)
            self.send_405()
            return
        if self.reject_origin(config, "DELETE", path):
            return
        server_era = config_server_era(config)
        session = config["session"]
        is_modern_request = server_era != "legacy" and request_era(None, self.headers) == "2026"
        if (
            server_era == "modern"
            or is_modern_request
            or not session["validate"]
            or not session["issueId"]
        ):
            self.record_405("DELETE", path, config)
            self.send_405()
            return
        if self.handle_pre_json_rpc_config(config):
            return
        auth_status = self.enforce_auth(config, None, None)
        if auth_status:
            self.record_auth_failure("DELETE", path, None, auth_status, config)
            return
        session_id = self.headers.get("mcp-session-id")
        failure = session_error(session_id)
        status = failure[0] if failure is not None else 200
        if failure is None and session_id:
            terminate_session(session_id)
        self.record(
            "DELETE",
            path,
            None,
            status,
            failure[1] if failure is not None else None,
            server_era=server_era,
            injected=["session.validate"],
        )
        if failure is not None:
            self.send_json(status, failure[1])
            return
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/scenario":
            self.handle_scenario_update()
            return

        if path == "/register":
            self.handle_register()
            return

        if path == "/authorize":
            self.handle_authorize_post()
            return

        if path == "/token":
            self.handle_token()
            return

        if path != "/mcp":
            self.send_text(404, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        # ValueError, not just JSONDecodeError: a body that is not UTF-8 raises
        # UnicodeDecodeError and an oversized integer literal a bare ValueError;
        # deeply nested input raises RecursionError.
        try:
            payload = json.loads(raw_body)
        except (ValueError, RecursionError):
            error = json_rpc_error(None, -32700, "Parse error")
            self.record("POST", path, raw_body.decode("utf-8", "replace"), 400, error)
            self.send_json(400, error)
            return

        # Developer-authored raw responses win over every other check (Accept,
        # delay, customHeader, httpStatus, auth, rejectLegacy, 2026 validation).
        # An invalid config falls through so the regular path reports the 400.
        try:
            early_config = self.request_config()
        except ValueError:
            early_config = None
        if early_config is not None:
            raw_rule = match_raw_response(early_config, payload)
            if raw_rule is not None:
                body_text = render_raw_body(raw_rule["body"], payload.get("id"))
                _, raw_headers = build_raw_response(raw_rule, body_text)
                self.record(
                    "POST",
                    path,
                    payload,
                    raw_rule["status"],
                    body_text,
                    raw_response=True,
                    response_headers=raw_headers,
                )
                self.send_raw_response(raw_rule, body_text)
                return

        # A JSON array (batch) or scalar is not a JSON-RPC request this server
        # accepts; answer with a JSON-RPC error instead of dying mid-request.
        if not isinstance(payload, dict):
            message = "Invalid Request"
            if isinstance(payload, list):
                message = "Invalid Request: JSON-RPC batch requests are not supported"
            error = json_rpc_error(None, ERR_INVALID_REQUEST, message)
            self.record("POST", path, payload, 400, error)
            self.send_json(400, error)
            return

        if early_config is not None and self.reject_origin(early_config, "POST", path, payload):
            return

        # serverEra="legacy" + a transport-level legacyModernProbe mode: the mode
        # decides the answer to a modern request, so the legacy Accept rule below
        # is not consulted for it.
        probe_reply = None
        if early_config is not None:
            probe_reply = legacy_modern_probe_reply(payload, early_config, self.exchange_headers())

        # 2026-07-28 is stateless and single-shot; a client may accept only
        # application/json. Legacy (2025) still requires both media types.
        accept = self.headers.get("Accept", "")
        era = request_era(payload, self.headers)
        # serverEra="legacy": 2026 markers are ignored, so the legacy Accept rule
        # applies and the 2026-only subscriptions/listen branch stays off.
        if config_server_era(early_config) == "legacy":
            era = "2025"
        rpc_method = payload.get("method")
        if probe_reply is not None:
            pass
        elif era == "2026":
            if rpc_method == SUBSCRIPTIONS_LISTEN_METHOD:
                if "text/event-stream" not in accept and "*/*" not in accept:
                    self.record(
                        "POST",
                        path,
                        payload,
                        400,
                        {"error": "Invalid Accept header. Expected TEXT_EVENT_STREAM"},
                    )
                    self.send_text(400, "Invalid Accept header. Expected TEXT_EVENT_STREAM")
                    return
            elif "application/json" not in accept and "*/*" not in accept:
                self.record("POST", path, payload, 400,
                            {"error": "Invalid Accept header. Expected APPLICATION_JSON"})
                self.send_text(400, "Invalid Accept header. Expected APPLICATION_JSON")
                return
        elif "application/json" not in accept or "text/event-stream" not in accept:
            text = "Invalid Accept headers. Expected TEXT_EVENT_STREAM and APPLICATION_JSON"
            self.record_text("POST", path, payload, 400, text, early_config)
            self.send_text(400, text)
            return

        try:
            config = self.request_config()
        except ValueError as error:
            self.record_text("POST", path, payload, 400, str(error))
            self.send_text(400, str(error))
            return
        if self.handle_pre_json_rpc_config(config, payload.get("method"), payload):
            return
        auth_status = self.enforce_auth(config, payload.get("method"), payload)
        if auth_status:
            self.record_auth_failure("POST", path, payload, auth_status, config)
            return

        if probe_reply is not None:
            self.send_reply(path, payload, probe_reply, config)
            return

        if era == "2026" and rpc_method == SUBSCRIPTIONS_LISTEN_METHOD:
            failure = modern_envelope_failure(payload, self.exchange_headers(), config)
            if failure is None:
                error = validate_subscriptions_listen(payload)
                failure = (400, error) if error is not None else None
            if failure is not None:
                self.record("POST", path, payload, failure[0], failure[1])
                self.send_json(failure[0], failure[1])
                return
            messages = [
                subscriptions_acknowledged_notification(payload),
                subscriptions_listen_result(payload.get("id"), config),
            ]
            self.record("POST", path, payload, 200, {"events": messages})
            self.send_sse_json_rpc(200, messages)
            return

        reply = handle_mcp_exchange(payload, config=config, headers=self.exchange_headers())
        self.send_reply(path, payload, reply, config)

    def exchange_headers(self) -> RequestHeaders:
        """Request headers as a dict (first copy wins) plus the repeated names."""
        headers = RequestHeaders(self.headers)
        seen: set[str] = set()
        duplicated: set[str] = set()
        for name in self.headers.keys():
            key = name.lower()
            if key in seen:
                duplicated.add(key)
            seen.add(key)
        headers.duplicated = frozenset(duplicated)
        return headers

    def send_reply(
        self,
        path: str,
        payload: Any,
        reply: dict[str, Any],
        config: dict[str, Any],
    ) -> None:
        """Write a handle_mcp_exchange reply (see _reply) and log it for /inspect."""
        if reply["delay"] > 0:
            time.sleep(reply["delay"])
        transport = reply["transport"]
        status = reply["status"]
        extra_headers = reply["headers"]
        response: Any = reply["body"]
        if transport == "sse":
            response = {"events": reply["events"]}
        elif transport == "text":
            response = {"text": reply["text"]} if reply["text"] else None
        elif transport == "disconnect":
            response = {"disconnect": True}
        elif transport == "auth-challenge":
            response = {"error": "invalid_token"}
        self.record(
            "POST",
            path,
            payload,
            status,
            response,
            response_headers=extra_headers if reply["injected"] else None,
            server_era=config_server_era(config),
            injected=reply["injected"],
        )
        if transport == "disconnect":
            self.drop_connection()
            return
        if transport == "auth-challenge":
            self.send_auth_challenge(include_scope=True)
            return
        if transport == "sse":
            self.send_sse_json_rpc(status, reply["events"], extra_headers)
            return
        if transport == "text":
            body = reply["text"].encode("utf-8")
            self.send_response(status)
            self.send_cors_headers()
            if body or reply["contentType"] != "text/plain;charset=UTF-8":
                self.send_header("Content-Type", reply["contentType"])
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra_headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
            return
        if response is None:
            self.send_response(status)
            self.send_cors_headers()
            for name, value in extra_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_json(status, response, extra_headers)

    def drop_connection(self) -> None:
        """Close the socket without writing a response."""
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def reject_origin(
        self,
        config: dict[str, Any],
        http_method: str,
        path: str,
        payload: Any = None,
    ) -> bool:
        """403 when allowedOrigins is set and the Origin header is not listed.

        An empty allow-list (the default) accepts every Origin.
        """
        allowed = config.get("allowedOrigins") or []
        origin = self.headers.get("Origin")
        if not allowed or origin is None or origin in allowed:
            return False
        # Spec: the 403 body MAY be a JSON-RPC error response that has no id.
        error = {
            "jsonrpc": "2.0",
            "error": {
                "code": ERR_INVALID_REQUEST,
                "message": f"Forbidden: Origin '{origin}' is not allowed",
            },
        }
        self.record(http_method, path, payload, 403, error, injected=["allowedOrigins"])
        self.send_json(403, error)
        return True

    def handle_scenario_update(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        content_type = self.headers.get("Content-Type", "")

        try:
            if "application/json" in content_type:
                payload = json.loads(raw_body or b"{}")
                if isinstance(payload, dict) and "action" in payload:
                    # Actions never change the active config.
                    self.send_json(200, scenario_action(payload))
                    return
                if isinstance(payload, dict) and "config" in payload:
                    config = set_active_config(payload["config"])
                    scenario = "custom"
                else:
                    raw_scenario = payload.get("scenario") if isinstance(payload, dict) else None
                    scenario = set_active_scenario(raw_scenario)
                    config = get_active_config()
            else:
                form = parse_qs(raw_body.decode("utf-8"))
                raw_scenario = form.get("scenario", ["ok"])[0]
                scenario = set_active_scenario(raw_scenario)
                config = get_active_config()
        except (ValueError, RecursionError) as error:
            if "application/json" in content_type:
                self.send_json(400, {"ok": False, "error": str(error)})
                return
            self.send_html(400, render_scenario_page(error=str(error)))
            return

        if "application/json" in content_type:
            self.send_json(200, {"ok": True, "scenario": scenario, "config": config})
            return

        self.send_html(200, render_scenario_page(message=f"Applied: {scenario}"))

    def send_json(
        self,
        status: int,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_sse_json_rpc(
        self,
        status: int,
        messages: list[dict[str, Any]],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        # Spec SHOULD: keep reverse proxies from buffering the event stream.
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        for message in messages:
            data = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"event: message\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()
        self.close_connection = True

    def send_raw_response(self, rule: dict[str, Any], body_text: str) -> None:
        """Send a developer-authored response verbatim (see build_raw_response)."""
        body, headers = build_raw_response(rule, body_text)
        self.send_response(rule["status"])
        # A user header replaces the default CORS header of the same name.
        self.send_cors_headers(skip={name.lower() for name in headers})
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def send_405(self, allow: str = "POST, OPTIONS") -> None:
        # Default Allow: the 2026-07-28 stateless transport, where only POST /mcp
        # is allowed.
        body = b"Method Not Allowed"
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_cors_headers()
        self.send_header("Allow", allow)
        self.send_header("Content-Type", "text/plain;charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/plain;charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/html;charset=UTF-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_cors_headers(self, skip: set[str] | None = None) -> None:
        """Default CORS headers. ``skip`` holds lowercase names the caller sends itself."""
        origin = self.headers.get("Origin") or "*"
        requested_headers = self.headers.get("Access-Control-Request-Headers")
        defaults = (
            ("Access-Control-Allow-Origin", origin),
            ("Access-Control-Allow-Credentials", "true"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE"),
            (
                "Access-Control-Allow-Headers",
                requested_headers
                or "authorization,content-type,accept,mcp-protocol-version,mcp-session-id,x-mcp-test-scenario,*",
            ),
            ("Access-Control-Expose-Headers", "Mcp-Session-Id,mcp-session-id,*"),
            ("Vary", "Origin"),
        )
        for name, value in defaults:
            if skip and name.lower() in skip:
                continue
            self.send_header(name, value)

    def request_scenario(self) -> str:
        return resolve_scenario(self.headers.get(TEST_SCENARIO_HEADER))

    def request_config(self) -> dict[str, Any]:
        query = parse_qs(urlparse(self.path).query)
        raw_config = query.get(CONFIG_QUERY_PARAM, [""])[0]
        if raw_config:
            return decode_config_token(raw_config)
        return resolve_config(self.headers.get(TEST_SCENARIO_HEADER))

    def handle_pre_json_rpc_config(
        self,
        config: dict[str, Any],
        method: str | None = None,
        payload: Any = None,
    ) -> bool:
        server = config["server"]
        if server["delayEnabled"]:
            time.sleep(server["delaySeconds"])
            if server["delaySeconds"] > 0:
                self._inspect_delay = [f"server.delaySeconds={server['delaySeconds']:g}"]

        def refuse(status: int, text: str, injected: str) -> bool:
            self.record_text(
                self.command, urlparse(self.path).path, payload, status, text, config, [injected]
            )
            self.send_text(status, text)
            return True

        custom_header = config.get("customHeader", {})
        if custom_header.get("enabled"):
            header_val = self.headers.get(custom_header.get("name", ""))
            if header_val is None:
                return refuse(401, "Unauthorized", "customHeader.enabled")
            if header_val != custom_header.get("value", ""):
                return refuse(403, "Forbidden", "customHeader.enabled")
            return False

        http_status = server["httpStatus"]
        target = server["target"]
        if http_status == 200:
            return False
        if target == "initialize" and method not in {None, "initialize"}:
            return False
        if http_status == 401:
            return refuse(401, "Unauthorized", "server.httpStatus=401")
        if http_status == 403:
            return refuse(403, "Forbidden", "server.httpStatus=403")
        return False

    def base_url(self) -> str:
        """Absolute origin of this server, honouring reverse-proxy headers."""
        proto = self.headers.get("X-Forwarded-Proto")
        if not proto:
            proto = "https" if self.headers.get("X-Forwarded-Ssl") == "on" else "http"
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        return f"{proto}://{host}"

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def read_form(self) -> dict[str, str]:
        raw = self.read_body()
        return {key: value[0] for key, value in parse_qs(raw.decode("utf-8", "replace")).items()}

    def enforce_auth(
        self,
        config: dict[str, Any],
        method: str | None,
        payload: Any,
    ) -> int:
        """Send a 401/403 challenge when the request lacks required auth.

        Returns the HTTP status that was sent, or 0 when the request may proceed.

        off         -> never gates. oauth -> every /mcp request needs a token.
        mixed-oauth -> only tools/call for a protected tool needs a token, so
        anonymous clients can still initialize, list, and call public tools.
        """
        auth = config.get("auth", {})
        mode = auth.get("mode", "off")
        if mode == "off":
            return 0

        needs_auth = False
        if mode == "oauth":
            needs_auth = True
        elif mode == "mixed-oauth" and method == "tools/call":
            params = payload.get("params") if isinstance(payload, dict) else None
            name = params.get("name") if isinstance(params, dict) else None
            needs_auth = name in auth.get("protectedTools", [])
        if not needs_auth:
            return 0

        # The scope parameter (spec SHOULD) is added on the strict modern path
        # only, so the legacy challenge stays byte-for-byte what it was.
        include_scope = (
            bool(config.get("strict2026", True))
            and config_server_era(config) != "legacy"
            and request_era(payload, self.headers) == "2026"
        )
        claims = verify_bearer_access_token(bearer_token(self.headers))
        if claims is None:
            self.send_auth_challenge(include_scope=include_scope)
            return 401
        if auth.get("enforceScope") and OAUTH_SCOPE not in str(claims.get("scope", "")).split():
            self.send_insufficient_scope()
            return 403
        return 0

    def send_insufficient_scope(self) -> None:
        resource_metadata = f"{self.base_url()}/.well-known/oauth-protected-resource"
        body = json.dumps(
            {
                "error": "insufficient_scope",
                "error_description": f"The access token lacks the required scope '{OAUTH_SCOPE}'.",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(403)
        self.send_cors_headers()
        self.send_header(
            "WWW-Authenticate",
            f'Bearer error="insufficient_scope", scope="{OAUTH_SCOPE}", '
            f'resource_metadata="{resource_metadata}"',
        )
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_auth_challenge(self, include_scope: bool = False) -> None:
        resource_metadata = f"{self.base_url()}/.well-known/oauth-protected-resource"
        body = json.dumps(
            {
                "error": "invalid_token",
                "error_description": (
                    "Authentication required. Obtain a bearer token from the "
                    "authorization server, then retry with an Authorization header."
                ),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(401)
        self.send_cors_headers()
        challenge = f'Bearer resource_metadata="{resource_metadata}"'
        if include_scope:
            challenge += f', scope="{OAUTH_SCOPE}"'
        self.send_header("WWW-Authenticate", challenge)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_cors_headers()
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_register(self) -> None:
        try:
            metadata = json.loads(self.read_body() or b"{}")
        except (ValueError, RecursionError):
            self.send_json(400, {"error": "invalid_client_metadata"})
            return
        if not isinstance(metadata, dict):
            metadata = {}
        self.send_json(201, register_client(metadata))

    def handle_authorize_get(self) -> None:
        params = {key: value[0] for key, value in parse_qs(urlparse(self.path).query).items()}
        self.send_html(200, render_authorize_page(params))

    def handle_authorize_post(self) -> None:
        form = self.read_form()
        redirect_uri = form.get("redirect_uri", "")
        state = form.get("state")
        if not redirect_uri:
            self.send_text(400, "Missing redirect_uri")
            return
        # A code is only issued once the user both logs in and consents.
        if form.get("action") != "approve" or form.get("consent") != "1":
            self.send_redirect(
                _append_query(redirect_uri, {"error": "access_denied", "state": state})
            )
            return
        code = create_auth_code(
            {
                "client_id": form.get("client_id", ""),
                "redirect_uri": redirect_uri,
                "code_challenge": form.get("code_challenge", ""),
                "code_challenge_method": form.get("code_challenge_method", "plain"),
                "scope": form.get("scope", OAUTH_SCOPE),
                "sub": DEMO_USER_SUB,
            }
        )
        self.send_redirect(_append_query(redirect_uri, {"code": code, "state": state}))

    def handle_token(self) -> None:
        form = self.read_form()
        grant_type = form.get("grant_type")
        if grant_type == "refresh_token":
            self.handle_refresh_grant(form)
            return
        if grant_type != "authorization_code":
            self.send_json(400, {"error": "unsupported_grant_type"})
            return
        entry = consume_auth_code(form.get("code", ""))
        if entry is None:
            self.send_json(
                400, {"error": "invalid_grant", "error_description": "Unknown or expired code"}
            )
            return
        if form.get("redirect_uri", "") != entry["redirect_uri"]:
            self.send_json(
                400, {"error": "invalid_grant", "error_description": "redirect_uri mismatch"}
            )
            return
        if not pkce_matches(
            form.get("code_verifier", ""),
            entry["code_challenge"],
            entry["code_challenge_method"],
        ):
            self.send_json(
                400, {"error": "invalid_grant", "error_description": "PKCE verification failed"}
            )
            return
        self.send_token_pair(entry["sub"], entry["client_id"], entry["scope"])

    def handle_refresh_grant(self, form: dict[str, str]) -> None:
        """RFC 6749 §6 — exchange a refresh token for a fresh access token.

        This is the only way back once an access token expires. Without it every
        token this server issues becomes permanently dead an hour after it was
        minted, so a client caching tokens across sessions inevitably converges
        on holding only expired ones — a 401 it can never clear.
        """
        claims = verify_access_token(form.get("refresh_token", ""))
        if claims is None or claims.get(TOKEN_TYPE_CLAIM) != TOKEN_TYPE_REFRESH:
            self.send_json(
                400,
                {
                    "error": "invalid_grant",
                    "error_description": "Unknown, expired, or non-refresh token",
                },
            )
            return

        client_id = claims.get("client_id", "")
        if form.get("client_id") and form.get("client_id") != client_id:
            self.send_json(
                400, {"error": "invalid_grant", "error_description": "client_id mismatch"}
            )
            return

        # A refresh may narrow the granted scope but never widen it (RFC 6749 §6).
        granted_scope = claims.get("scope", OAUTH_SCOPE)
        requested_scope = form.get("scope")
        if requested_scope and set(requested_scope.split()) - set(granted_scope.split()):
            self.send_json(
                400,
                {
                    "error": "invalid_scope",
                    "error_description": f"Scope exceeds the original grant '{granted_scope}'",
                },
            )
            return

        self.send_token_pair(
            claims.get("sub", DEMO_USER_SUB), client_id, requested_scope or granted_scope
        )

    def send_token_pair(self, sub: str, client_id: str, scope: str) -> None:
        """Issue an access + refresh pair for one identity."""
        base = self.base_url()
        identity = {
            "iss": base,
            "sub": sub,
            "aud": f"{base}/mcp",
            "client_id": client_id,
            "scope": scope,
        }
        self.send_json(
            200,
            {
                "access_token": issue_access_token(
                    {**identity, TOKEN_TYPE_CLAIM: TOKEN_TYPE_ACCESS}
                ),
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "refresh_token": issue_refresh_token(identity),
                "scope": scope,
            },
        )

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"{SERVER_NAME} listening on 0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
