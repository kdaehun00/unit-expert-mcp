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
    ("2026-07-28", "2026-07-28 (모던 버전 - handshake 응답으로는 무효)"),
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
INSPECT_LOG: deque[dict[str, Any]] = deque(maxlen=50)
INSPECT_SEQ = 0
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
) -> None:
    """Append one /mcp exchange to the inspection ring buffer.

    ``injected`` names the config options that shaped this response (for example
    ``toolCall.mode=sse``), so /inspect shows why a response looks the way it does.
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
    with INSPECT_LOCK:
        INSPECT_SEQ += 1
        INSPECT_LOG.appendleft(
            {
                "seq": INSPECT_SEQ,
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
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
            }
        )


def inspect_snapshot() -> list[dict[str, Any]]:
    with INSPECT_LOCK:
        return list(INSPECT_LOG)


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
    "ok": "정상 Unit Expert 도구 목록을 반환합니다.",
    "auth-401": "JSON-RPC 처리 전에 401 Unauthorized를 반환합니다.",
    "auth-403": "JSON-RPC 처리 전에 403 Forbidden을 반환합니다.",
    "unsupported-min-version": "최소 지원 버전보다 낮은 protocolVersion 2024-11-05를 반환합니다.",
    "tools-list-error": "tools/list에서 JSON-RPC error를 반환합니다.",
    "tools-list-null": "tools/list에서 tools: null을 반환합니다.",
    "tools-list-empty": "tools/list에서 tools: []를 반환합니다.",
    "duplicate-tool-name": "동일한 name을 가진 중복 tool을 반환합니다.",
    "too-many-tools": "도구 21개를 반환합니다.",
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
        "tools/list 도구 구성",
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

TOOL_ERROR_GROUPS = (
    (
        "툴 이름 조건",
        "복수 선택 가능",
        (
            "invalid-tool-name-char",
            "invalid-tool-name-length",
            "forbidden-kakao-name",
        ),
    ),
    (
        "툴 필수 속성",
        "복수 선택 가능",
        (
            "missing-name",
            "missing-description",
            "missing-input-schema",
            "missing-annotations",
        ),
    ),
    (
        "툴 설명 조건",
        "복수 선택 가능",
        (
            "long-description",
            "missing-service-name-in-description",
        ),
    ),
    (
        "툴 annotations 조건",
        "복수 선택 가능",
        (
            "incomplete-annotations",
        ),
    ),
)

# Scenarios that only make sense in the 2025 (handshake) transport. The 2026
# transport has no initialize step, so unsupported-min-version has no meaning.
ERA_2025_ONLY_SCENARIOS = ("unsupported-min-version",)

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
    "no-modern-version": "supportedVersions에 2026-07-28 없음(레거시 버전만)",
    "empty-versions": "supportedVersions: []",
    "missing-versions": "supportedVersions 필드 없음",
    "json-rpc-error": "HTTP 200 + JSON-RPC error(discover.errorCode)",
    "header-mismatch": "HTTP 400 + -32020",
    "unsupported-version": "HTTP 400 + -32022(data.supported/requested)",
    "http-401": "HTTP 401 + WWW-Authenticate",
    "http-403": "HTTP 403",
    "http-5xx": "HTTP 5xx(discover.httpStatus), 본문은 평문",
    "sse": "text/event-stream: 알림 1건 뒤 정상 응답",
    "sse-no-response": "text/event-stream: 알림만 보내고 응답 없이 종료",
    "id-mismatch": "요청과 다른 id의 정상 응답",
    "invalid-json": "HTTP 200 + JSON이 아닌 본문",
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
    "method-not-found": "레거시 경로로 처리, server/discover는 HTTP 200 + -32601 (기본)",
    "invalid-params": "모르는 메서드에 HTTP 200 + -32602 (Python SDK 1.x 형태)",
    "http-400": "레거시가 아닌 MCP-Protocol-Version 헤더면 HTTP 400 + JSON-RPC -32600 (Python SDK 1.x 형태)",
    "http-404": "HTTP 404, JSON-RPC 본문 없음",
    "empty-400": "HTTP 400, 본문 없음",
    "timeout": "legacyModernProbe.delaySeconds 동안 무응답 후 연결 종료",
}
# Modes decided before the legacy Accept rule (they answer on the transport level).
LEGACY_MODERN_PROBE_TRANSPORT_MODES = ("http-400", "http-404", "empty-400", "timeout")

# toolCall.mode: tools/call injection shared by both eras.
TOOL_CALL_MODES = {
    "normal": "정상 실행",
    "is-error": "isError: true 결과",
    "json-rpc-error": "HTTP 200 + JSON-RPC error(toolCall.errorCode)",
    "http-status": "HTTP toolCall.httpStatus, 본문은 평문",
    "sse": "text/event-stream: 알림 toolCall.sseNotificationCount건 뒤 최종 응답",
    "id-mismatch": "요청과 다른 id의 정상 응답",
    "empty": "HTTP 200 + 빈 본문",
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


def tools_for_scenario(scenario: str) -> dict[str, Any] | None:
    match scenario:
        case "tools-list-null":
            return {"tools": None}
        case "tools-list-empty":
            return {"tools": []}
        case "duplicate-tool-name":
            return {"tools": [valid_tool("search_place"), valid_tool("search_place")]}
        case "too-many-tools":
            return {"tools": [valid_tool(f"tool_{index}") for index in range(21)]}
        case "invalid-tool-name-char":
            return {"tools": [valid_tool("search place!")]}
        case "invalid-tool-name-length":
            return {"tools": [valid_tool("a" * 129)]}
        case "missing-name":
            tool = valid_tool("search_place")
            tool.pop("name")
            return {"tools": [tool]}
        case "missing-description":
            tool = valid_tool("search_place")
            tool.pop("description")
            return {"tools": [tool]}
        case "missing-input-schema":
            tool = valid_tool("search_place")
            tool.pop("inputSchema")
            return {"tools": [tool]}
        case "missing-annotations":
            tool = valid_tool("search_place")
            tool.pop("annotations")
            return {"tools": [tool]}
        case "forbidden-kakao-name":
            return {"tools": [valid_tool("kakao_search")]}
        case "long-description":
            return {"tools": [valid_tool("search_place", long_description())]}
        case "missing-service-name-in-description":
            return {"tools": [valid_tool("search_place", "Search places nearby.")]}
        case "incomplete-annotations":
            tool = valid_tool("search_place")
            tool["annotations"] = {
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            }
            return {"tools": [tool]}
        case _:
            return None


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
    reply["delay"] = reply["delay"] + delay
    reply["injected"] = reply["injected"] + labels
    return reply


def modern_tools_list_reply(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    request_id = payload.get("id")
    params = payload.get("params")
    tools_list = config["toolsList"]
    if tools_list["mode"] == "json-rpc-error":
        return _reply(200, json_rpc_error(request_id, ERR_INTERNAL, "Injected tools/list failure"))
    scenario_tools = tools_for_config(config)
    result = dict(scenario_tools or {"tools": modern_tool_definitions(config)})
    result, error, labels = paginate_tools_result(result, config, params, request_id)
    if error is not None:
        status = 200
        if error["error"]["code"] == ERR_INVALID_PARAMS:
            status = config["modern"]["invalidParamsHttpStatus"]
        return _reply(status, error, injected=labels)
    cursor = params.get("cursor") if isinstance(params, dict) else None
    if cursor is not None and tools_list["pageSize"] <= 0:
        # This server issued no cursor, so any cursor is one it does not know.
        return _invalid_params_reply(request_id, config, "Invalid cursor")
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
        return _invalid_params_reply(request_id, config, f"Unknown tool: {name}")
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

    if name == MRTR_TOOL:
        return confirm_action_reply(request_id, params, arguments, request_meta(payload), config)
    if name == HEADER_PARAMS_TOOL:
        result = echo_header_params_result(arguments)
    else:
        result = call_tool(params)
    return _reply(200, json_rpc_result_2026(request_id, result, config))


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
        if not config["session"]["issueId"]:
            return _reply(200, json_rpc_result(request_id, result), injected=["session.issueId=false"])
        session_id = str(uuid.uuid4())
        SESSIONS.add(session_id)
        return _reply(200, json_rpc_result(request_id, result), {"Mcp-Session-Id": session_id})

    if method == "notifications/initialized":
        return _reply(202)

    if method == "ping":
        if is_notification:
            return _reply(202)
        return _reply(200, json_rpc_result(request_id, {}))

    if method == "tools/list":
        if config["toolsList"]["mode"] == "json-rpc-error":
            return _reply(200, json_rpc_error(request_id, ERR_INTERNAL, "Injected tools/list failure"))
        scenario_tools = tools_for_config(config)
        result = scenario_tools or {"tools": tools(config["mcp"]["serviceName"])}
        result, error, labels = paginate_tools_result(
            result, config, payload.get("params"), request_id
        )
        if error is not None:
            return _reply(200, error, injected=labels)
        return _reply(200, json_rpc_result(request_id, result), injected=labels)

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
        reply = _reply(status, body, extra_headers, injected=["strict2026=false"])
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


def scenario_group_label(scenario: str) -> str:
    for label, scenarios in SCENARIO_GROUPS:
        if scenario in scenarios:
            return label
    return "기타"


def render_inspect_page() -> str:
    """Live viewer for recent /mcp exchanges (request headers + body, response).

    Plain (non f-string) template so the embedded JS braces need no escaping.
    The page polls /inspect/log and re-renders on change.
    """
    tool_names = [tool["name"] for tool in tools()]
    return _INSPECT_PAGE.replace("__CONVERT_TOOLS__", json.dumps(tool_names))


_INSPECT_PAGE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unit Expert MCP · Inspect</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --panel-soft: #f1f5f4;
      --text: #17201d;
      --muted: #62706b;
      --line: #d9e0dd;
      --line-strong: #b8c4bf;
      --accent: #0f766e;
      --accent-strong: #115e59;
      --accent-soft: #dff3ef;
      --danger: #b42318;
      --danger-soft: #fde7e4;
      --warn: #8a5a00;
      --warn-soft: #fff2cc;
      --code-bg: #101818;
      --code-text: #d9f2ed;
      --shadow: 0 10px 30px rgba(20, 34, 30, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }
    button, input, select, textarea { font: inherit; }
    button {
      border: 1px solid var(--line-strong);
      background: #ffffff;
      color: var(--text);
      border-radius: 6px;
      padding: 8px 12px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 650;
    }
    button:hover { border-color: var(--accent); color: var(--accent-strong); }
    button:disabled { opacity: 0.55; cursor: default; }
    .primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #ffffff;
    }
    .primary:hover { background: var(--accent-strong); color: #ffffff; }
    .quiet { background: transparent; }
    .small { padding: 6px 9px; font-size: 12px; }
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      background: rgba(246, 247, 249, 0.96);
      backdrop-filter: blur(10px);
      border-bottom: 1px solid var(--line);
    }
    .topbar {
      max-width: 1440px;
      margin: 0 auto;
      padding: 14px 20px;
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }
    .title { display: flex; flex-direction: column; min-width: 0; }
    h1 { margin: 0; font-size: 18px; line-height: 1.2; }
    .subtitle { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .top-actions { margin-left: auto; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .live-toggle {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      padding: 7px 10px;
      font-size: 12px;
      background: #ffffff;
      cursor: pointer;
    }
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #14835b;
      display: inline-block;
    }
    .status-dot.paused { background: #c88200; }
    main {
      max-width: 1440px;
      margin: 0 auto;
      padding: 18px 20px 56px;
      display: grid;
      grid-template-columns: minmax(420px, 0.9fr) minmax(460px, 1.1fr);
      gap: 16px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
    }
    .panel-head {
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      gap: 10px;
      justify-content: space-between;
    }
    .panel-head h2 { margin: 0; font-size: 15px; }
    .panel-body { padding: 16px; }
    .verify { align-self: start; position: sticky; top: 82px; }
    .stack { display: flex; flex-direction: column; gap: 14px; }
    .field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
    .field label, .editor-title {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    input, select, textarea {
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: #ffffff;
      color: var(--text);
      padding: 9px 10px;
      min-width: 0;
    }
    input:focus, select:focus, textarea:focus {
      outline: 2px solid var(--accent-soft);
      border-color: var(--accent);
    }
    textarea {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.5;
      resize: vertical;
      width: 100%;
    }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
    .tool-fields { display: grid; grid-template-columns: 1.5fr 0.8fr 0.8fr 0.8fr; gap: 10px; }
    .tool-fields.hidden { display: none; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .cache-control {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px 12px;
      background: var(--panel-soft);
    }
    .cache-copy { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
    .cache-copy strong { font-size: 13px; }
    .cache-copy span { color: var(--muted); font-size: 12px; }
    .switch { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; white-space: nowrap; }
    .switch input { width: 16px; height: 16px; padding: 0; }
    details.editor {
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    details.editor > summary {
      list-style: none;
      cursor: pointer;
      padding: 11px 12px;
      font-weight: 700;
      font-size: 13px;
      background: var(--panel-soft);
    }
    details.editor > summary::-webkit-details-marker { display: none; }
    .editor-body { padding: 12px; display: grid; grid-template-columns: 1fr; gap: 12px; }
    .result-box {
      min-height: 170px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfc;
      padding: 14px;
      display: grid;
      gap: 12px;
      align-content: start;
    }
    .result-title { font-weight: 750; font-size: 14px; }
    .result-text { color: var(--muted); font-size: 13px; }
    .metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #ffffff;
    }
    .metric dt { color: var(--muted); font-size: 11px; font-weight: 700; margin: 0 0 5px; }
    .metric dd { margin: 0; font-size: 18px; font-weight: 800; }
    .status-line {
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--panel-soft);
      color: var(--text);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .status-line.ok { background: #e1f5eb; color: #14532d; }
    .status-line.err { background: var(--danger-soft); color: var(--danger); }
    .status-line.warn { background: var(--warn-soft); color: var(--warn); }
    .log-panel { grid-column: 1 / -1; }
    .log-toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }
    .log-list { display: grid; gap: 8px; }
    .empty {
      border: 1px dashed var(--line-strong);
      border-radius: 8px;
      padding: 28px;
      text-align: center;
      color: var(--muted);
      background: #fbfcfc;
    }
    .row {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      overflow: hidden;
    }
    .row-head {
      display: grid;
      grid-template-columns: 74px 72px minmax(150px, 1fr) 96px 76px;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      min-height: 50px;
    }
    .row:hover { border-color: var(--line-strong); }
    .row-body {
      display: none;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      border-top: 1px solid var(--line);
      padding: 12px;
      background: #fbfcfc;
    }
    .row.open .row-body { display: grid; }
    .time, .muted { color: var(--muted); font-size: 12px; }
    .method {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-weight: 750;
      overflow-wrap: anywhere;
    }
    .badge {
      justify-self: start;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 11px;
      font-weight: 800;
      white-space: nowrap;
    }
    .era-2026 { background: var(--accent-soft); color: var(--accent-strong); }
    .era-2025 { background: #eceff3; color: #3d4955; }
    .ok { background: #e1f5eb; color: #14532d; }
    .err { background: var(--danger-soft); color: var(--danger); }
    .raw { background: #fff1d6; color: #8a4b00; }
    .method .badge.raw { display: inline-block; margin-left: 6px; vertical-align: middle; }
    .pane { min-width: 0; }
    .pane h3 { margin: 0 0 7px; font-size: 12px; color: var(--muted); }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.5;
      color: var(--code-text);
      background: var(--code-bg);
      border-radius: 8px;
      padding: 12px;
      max-height: 460px;
      overflow: auto;
    }
    .hdr-line { color: var(--code-text); }
    .hdr-line b { color: #8ee8d3; }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .verify { position: static; }
      .row-head { grid-template-columns: 62px 66px minmax(120px, 1fr) 82px 70px; }
      .row-body { grid-template-columns: 1fr; }
      .tool-fields, .grid-3 { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      .topbar { align-items: flex-start; flex-direction: column; }
      .top-actions { margin-left: 0; width: 100%; }
      .top-actions button, .live-toggle { flex: 1; justify-content: center; }
      main { padding: 12px; }
      .grid-2, .metrics { grid-template-columns: 1fr; }
      .row-head { grid-template-columns: 1fr 1fr; }
      .row-head .method { grid-column: 1 / -1; }
      .row-head .small { justify-self: stretch; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div class="title">
        <h1>MCP 요청 검사</h1>
        <div class="subtitle" id="meta">최근 /mcp 요청을 표시합니다.</div>
      </div>
      <div class="top-actions">
        <label class="live-toggle">
          <span class="status-dot" id="dot"></span>
          <span id="liveLabel">실시간</span>
          <input type="checkbox" id="liveToggle" checked>
        </label>
        <button id="refreshBtn" type="button">새로고침</button>
        <button id="clearBtn" type="button">화면 비우기</button>
      </div>
    </div>
  </header>
  <main>
    <section class="panel verify" aria-label="검증 실행">
      <div class="panel-head">
        <h2>검증 실행</h2>
        <button class="quiet small" id="fillBtn" type="button">초기화</button>
      </div>
      <div class="panel-body stack">
        <div class="field">
          <label for="preset">검증 항목</label>
          <select id="preset">
            <option value="tools-list-cache">tools/list 캐시</option>
            <option value="tools-list">tools/list 정상</option>
            <option value="tools-call">tools/call 정상</option>
            <option value="missing-meta-version">_meta protocolVersion 누락</option>
            <option value="header-mismatch">Mcp-Method 헤더 불일치</option>
            <option value="subscriptions-listen">subscriptions/listen ack</option>
          </select>
        </div>
        <div class="field">
          <label for="cUrl">대상 URL</label>
          <input id="cUrl" type="text" value="/mcp" spellcheck="false">
        </div>
        <div class="grid-2">
          <div class="field">
            <label for="cMethod">메서드</label>
            <select id="cMethod">
              <option value="server/discover">server/discover</option>
              <option value="tools/list">tools/list</option>
              <option value="tools/call">tools/call</option>
              <option value="subscriptions/listen">subscriptions/listen</option>
              <option value="ping">ping</option>
            </select>
          </div>
          <div class="field">
            <label for="clientName">clientInfo.name</label>
            <input id="clientName" type="text" value="inspect-console" spellcheck="false">
          </div>
        </div>
        <div class="tool-fields" id="callFields">
          <div class="field">
            <label for="cTool">도구</label>
            <select id="cTool"></select>
          </div>
          <div class="field" id="fVal">
            <label for="cValue">value</label>
            <input id="cValue" type="number" value="1">
          </div>
          <div class="field" id="fFrom">
            <label for="cFrom">from</label>
            <input id="cFrom" type="text" value="m">
          </div>
          <div class="field" id="fTo">
            <label for="cTo">to</label>
            <input id="cTo" type="text" value="cm">
          </div>
        </div>
        <div class="cache-control">
          <div class="cache-copy">
            <strong>SDK 캐시 시뮬레이션</strong>
            <span>tools/list 응답의 ttlMs가 양수일 때 두 번째 요청을 생략합니다.</span>
          </div>
          <label class="switch">
            <input type="checkbox" id="clientCacheEnabled">
            사용
          </label>
        </div>
        <details class="editor">
          <summary>요청 원문</summary>
          <div class="editor-body">
            <div class="field">
              <span class="editor-title">헤더</span>
              <textarea id="hdrBox" spellcheck="false" rows="6"></textarea>
            </div>
            <div class="field">
              <span class="editor-title">본문</span>
              <textarea id="bodyBox" spellcheck="false" rows="12"></textarea>
            </div>
          </div>
        </details>
        <div class="actions">
          <button class="primary" id="sendBtn" type="button">1회 보내기</button>
          <button id="sendTwiceBtn" type="button">2회 연속 보내기</button>
        </div>
        <div class="status-line" id="callNote">검증 항목을 선택하면 요청 원문이 채워집니다.</div>
      </div>
    </section>

    <section class="panel" aria-label="최근 결과">
      <div class="panel-head">
        <h2>최근 결과</h2>
      </div>
      <div class="panel-body">
        <div class="result-box">
          <div>
            <div class="result-title" id="resultTitle">아직 실행 전</div>
            <div class="result-text" id="resultText">요청을 보내면 HTTP 상태, 캐시 hit, 로그 반영 여부가 표시됩니다.</div>
          </div>
          <dl class="metrics">
            <div class="metric">
              <dt>실행</dt>
              <dd id="metricRuns">0</dd>
            </div>
            <div class="metric">
              <dt>서버 도달</dt>
              <dd id="metricHits">0</dd>
            </div>
            <div class="metric">
              <dt>캐시 hit</dt>
              <dd id="metricCache">0</dd>
            </div>
          </dl>
          <pre id="lastResponse">-</pre>
        </div>
      </div>
    </section>

    <section class="panel log-panel" aria-label="요청 내역">
      <div class="panel-head">
        <h2>요청 내역</h2>
        <div class="log-toolbar" id="logSummary">표시 0건</div>
      </div>
      <div class="panel-body">
        <div class="empty" id="empty">아직 기록된 /mcp 요청이 없습니다.</div>
        <div class="log-list" id="list"></div>
      </div>
    </section>
  </main>
  <script>
    const CONVERT_TOOLS = __CONVERT_TOOLS__;
    const protocolVersion2026 = "2026-07-28";
    const cacheTarget = "/mcp?cfg=eyJwcm90b2NvbEVyYSI6IjIwMjYiLCJ0b29sc0xpc3QiOnsiY2FjaGVUdGxNcyI6NjAwMDB9fQ";

    const listEl = document.getElementById("list");
    const emptyEl = document.getElementById("empty");
    const metaEl = document.getElementById("meta");
    const logSummary = document.getElementById("logSummary");
    const dotEl = document.getElementById("dot");
    const liveToggle = document.getElementById("liveToggle");
    const liveLabel = document.getElementById("liveLabel");
    const preset = document.getElementById("preset");
    const cMethod = document.getElementById("cMethod");
    const cTool = document.getElementById("cTool");
    const callFields = document.getElementById("callFields");
    const sendBtn = document.getElementById("sendBtn");
    const sendTwiceBtn = document.getElementById("sendTwiceBtn");
    const fillBtn = document.getElementById("fillBtn");
    const callNote = document.getElementById("callNote");
    const cValue = document.getElementById("cValue");
    const cFrom = document.getElementById("cFrom");
    const cTo = document.getElementById("cTo");
    const hdrBox = document.getElementById("hdrBox");
    const bodyBox = document.getElementById("bodyBox");
    const cUrl = document.getElementById("cUrl");
    const clientName = document.getElementById("clientName");
    const clientCacheEnabled = document.getElementById("clientCacheEnabled");
    const resultTitle = document.getElementById("resultTitle");
    const resultText = document.getElementById("resultText");
    const metricRuns = document.getElementById("metricRuns");
    const metricHits = document.getElementById("metricHits");
    const metricCache = document.getElementById("metricCache");
    const lastResponse = document.getElementById("lastResponse");

    const openRows = new Set();
    const clientCache = new Map();
    let hideBefore = 0;
    let lastSignature = "";
    let runCount = 0;
    let serverHitCount = 0;
    let cacheHitCount = 0;

    CONVERT_TOOLS.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      cTool.appendChild(opt);
    });
    cTool.value = "convert_length";

    function esc(value) {
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function prettyRaw(value) {
      if (typeof value === "string") return value;
      try { return JSON.stringify(value, null, 2); } catch (e) { return String(value); }
    }

    function pretty(value) {
      return esc(prettyRaw(value));
    }

    function readJson(text) {
      try { return JSON.parse(text); } catch (e) { return null; }
    }

    function meta() {
      return {
        "io.modelcontextprotocol/protocolVersion": protocolVersion2026,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {
          name: clientName.value.trim() || "inspect-console",
          version: "1.0"
        }
      };
    }

    function bodyFor(method) {
      const params = { _meta: meta() };
      if (method === "tools/call") {
        const tool = cTool.value;
        params.name = tool;
        params.arguments = tool === "list_supported_units"
          ? {}
          : {
              value: Number(cValue.value),
              from_unit: cFrom.value.trim(),
              to_unit: cTo.value.trim()
            };
      }
      if (method === "subscriptions/listen") {
        params.notifications = { toolsListChanged: true };
      }
      return { jsonrpc: "2.0", id: 1, method, params };
    }

    function headersFor(method) {
      const headers = {
        "Content-Type": "application/json",
        "Accept": method === "subscriptions/listen" ? "text/event-stream" : "application/json",
        "MCP-Protocol-Version": protocolVersion2026,
        "Mcp-Method": method
      };
      if (method === "tools/call") headers["Mcp-Name"] = cTool.value;
      return headers;
    }

    function syncToolFields() {
      const isCall = cMethod.value === "tools/call";
      callFields.classList.toggle("hidden", !isCall);
      const needsArgs = isCall && cTool.value !== "list_supported_units";
      document.getElementById("fVal").style.display = needsArgs ? "" : "none";
      document.getElementById("fFrom").style.display = needsArgs ? "" : "none";
      document.getElementById("fTo").style.display = needsArgs ? "" : "none";
    }

    function writeDraft() {
      const method = cMethod.value;
      const headers = headersFor(method);
      const body = bodyFor(method);
      hdrBox.value = Object.keys(headers).map((k) => `${k}: ${headers[k]}`).join("\\n");
      bodyBox.value = JSON.stringify(body, null, 2);
      syncToolFields();
    }

    function applyPreset() {
      const value = preset.value;
      clientCacheEnabled.checked = value === "tools-list-cache";
      cUrl.value = value === "tools-list-cache" ? cacheTarget : "/mcp";
      if (value === "tools-call") cMethod.value = "tools/call";
      else if (value === "subscriptions-listen") cMethod.value = "subscriptions/listen";
      else cMethod.value = "tools/list";
      writeDraft();
      if (value === "missing-meta-version") {
        const body = readJson(bodyBox.value);
        if (body && body.params && body.params._meta) {
          delete body.params._meta["io.modelcontextprotocol/protocolVersion"];
          bodyBox.value = JSON.stringify(body, null, 2);
        }
      }
      if (value === "header-mismatch") {
        const headers = parseHeaders(hdrBox.value);
        headers["Mcp-Method"] = "tools/call";
        hdrBox.value = Object.keys(headers).map((k) => `${k}: ${headers[k]}`).join("\\n");
      }
      setNote("warn", "요청 원문이 갱신됐습니다.");
    }

    function parseHeaders(text) {
      const headers = {};
      text.split("\\n").forEach((line) => {
        const trimmed = line.trim();
        if (!trimmed) return;
        const index = trimmed.indexOf(":");
        if (index === -1) return;
        const key = trimmed.slice(0, index).trim();
        const value = trimmed.slice(index + 1).trim();
        if (key) headers[key] = value;
      });
      return headers;
    }

    function normalizedCacheKey(target, headers, payload) {
      if (!payload || payload.method !== "tools/list") return "";
      const params = payload.params && typeof payload.params === "object"
        ? JSON.parse(JSON.stringify(payload.params))
        : {};
      return JSON.stringify({
        target,
        protocolVersion: headers["MCP-Protocol-Version"] || headers["mcp-protocol-version"] || "",
        method: payload.method,
        params
      });
    }

    function setNote(kind, text) {
      callNote.className = `status-line ${kind || ""}`.trim();
      callNote.textContent = text;
    }

    function updateMetrics() {
      metricRuns.textContent = String(runCount);
      metricHits.textContent = String(serverHitCount);
      metricCache.textContent = String(cacheHitCount);
    }

    function setResult(title, text, response) {
      resultTitle.textContent = title;
      resultText.textContent = text;
      lastResponse.textContent = response === undefined ? "-" : prettyRaw(response);
    }

    async function sendRequest() {
      const headers = parseHeaders(hdrBox.value);
      const rawBody = bodyBox.value;
      const target = cUrl.value.trim() || "/mcp";
      const parsedBody = readJson(rawBody);
      const cacheKey = clientCacheEnabled.checked
        ? normalizedCacheKey(target, headers, parsedBody)
        : "";
      const sameOrigin = target.startsWith("/") || target.startsWith(location.origin);
      runCount += 1;
      updateMetrics();

      if (cacheKey) {
        const cached = clientCache.get(cacheKey);
        if (cached && cached.expiresAt > Date.now()) {
          cacheHitCount += 1;
          updateMetrics();
          const seconds = Math.ceil((cached.expiresAt - Date.now()) / 1000);
          setNote("ok", `CLIENT CACHE HIT · 서버 요청 생략 · ttl 남음 ${seconds}초`);
          setResult("클라이언트 캐시 hit", "두 번째 요청은 서버에 보내지 않았습니다.", cached.response);
          return { sent: false, response: cached.response };
        }
        clientCache.delete(cacheKey);
      }

      serverHitCount += 1;
      updateMetrics();
      const res = await fetch(target, { method: "POST", headers, body: rawBody });
      const text = await res.text();
      const responseJson = readJson(text);
      const response = responseJson || text;
      const ttlMs = responseJson && responseJson.result ? Number(responseJson.result.ttlMs || 0) : 0;
      if (cacheKey && res.ok && ttlMs > 0) {
        clientCache.set(cacheKey, { expiresAt: Date.now() + ttlMs, response });
      }
      const label = sameOrigin ? "로그에 기록됨" : "외부 URL";
      const cacheLabel = cacheKey ? ` · ttlMs ${ttlMs}` : "";
      setNote(res.ok ? "ok" : "err", `HTTP ${res.status} · ${label}${cacheLabel}`);
      setResult(`HTTP ${res.status}`, cacheKey && ttlMs > 0 ? `${ttlMs}ms 캐시 저장` : label, response);
      lastSignature = "";
      poll();
      return { sent: true, response };
    }

    async function runSend(count) {
      sendBtn.disabled = true;
      sendTwiceBtn.disabled = true;
      try {
        for (let index = 0; index < count; index += 1) {
          await sendRequest();
        }
      } catch (e) {
        setNote("err", "요청 실패: " + (e && e.message ? e.message : e));
        setResult("요청 실패", "브라우저 또는 CORS에서 차단됐을 수 있습니다.", e && e.stack ? e.stack : String(e));
      } finally {
        sendBtn.disabled = false;
        sendTwiceBtn.disabled = false;
      }
    }

    function headerLines(headers) {
      const keys = Object.keys(headers || {});
      if (!keys.length) return "<span class=\\"hdr-line\\">(없음)</span>";
      return keys.map((k) => `<div class="hdr-line"><b>${esc(k)}</b>: ${esc(headers[k])}</div>`).join("");
    }

    function statusText(exchange) {
      // status 0: the server closed the connection without a response (disconnect).
      if (!exchange.status) return "연결 종료";
      const errorCode = exchange.responseBody && exchange.responseBody.error
        ? exchange.responseBody.error.code
        : null;
      return errorCode === null || errorCode === undefined
        ? String(exchange.status)
        : `${exchange.status} / ${errorCode}`;
    }

    function render(exchanges) {
      const visible = exchanges.filter((x) => x.seq > hideBefore);
      emptyEl.style.display = visible.length ? "none" : "block";
      logSummary.textContent = `표시 ${visible.length}건 · 버퍼 ${exchanges.length}건`;
      metaEl.textContent = `최근 /mcp 요청 ${visible.length}건`;
      listEl.innerHTML = visible.map((x) => {
        const ok = x.status >= 200 && x.status < 400;
        const open = openRows.has(x.seq) ? " open" : "";
        return `
        <article class="row${open}" data-seq="${x.seq}">
          <div class="row-head">
            <span class="time">${esc(x.time)}</span>
            <span class="badge ${x.era === "2026" ? "era-2026" : "era-2025"}"${x.serverEra && x.serverEra !== "both" ? ' title="serverEra=' + esc(x.serverEra) + ' 모드로 처리됨"' : ""}>${esc(x.era)}${x.serverEra && x.serverEra !== "both" ? " → " + esc(x.serverEra) : ""}</span>
            <span class="method">${esc(x.method || x.httpMethod)}${x.rawResponse ? ' <span class="badge raw" title="개발자가 지정한 고정 응답(raw response)">RAW</span>' : ""}${(x.injected || []).length ? ' <span class="badge raw" title="' + esc(x.injected.join(", ")) + '">주입 ' + x.injected.length + '</span>' : ""}</span>
            <span class="badge ${ok ? "ok" : "err"}">${esc(statusText(x))}</span>
            <button class="small detail-toggle" type="button">${open ? "닫기" : "상세"}</button>
          </div>
          <div class="row-body">
            <div class="pane">
              <h3>요청</h3>
              <pre>${headerLines(x.requestHeaders)}\\n\\n${pretty(x.requestBody)}</pre>
            </div>
            <div class="pane">
              <h3>응답${x.rawResponse ? " (RAW)" : ""}${(x.injected || []).length ? " · 주입: " + esc(x.injected.join(", ")) : ""}</h3>
              <pre>${x.responseHeaders && Object.keys(x.responseHeaders).length ? headerLines(x.responseHeaders) + "\\n\\n" : ""}${pretty(x.responseBody)}</pre>
            </div>
          </div>
        </article>`;
      }).join("");
    }

    listEl.addEventListener("click", (event) => {
      const button = event.target.closest(".detail-toggle");
      if (!button) return;
      const row = button.closest(".row");
      if (!row) return;
      const seq = Number(row.dataset.seq);
      if (openRows.has(seq)) openRows.delete(seq);
      else openRows.add(seq);
      row.classList.toggle("open", openRows.has(seq));
      button.textContent = openRows.has(seq) ? "닫기" : "상세";
    });

    document.getElementById("clearBtn").addEventListener("click", () => {
      const rows = listEl.querySelectorAll(".row");
      let maxSeq = hideBefore;
      rows.forEach((row) => { maxSeq = Math.max(maxSeq, Number(row.dataset.seq)); });
      hideBefore = maxSeq;
      openRows.clear();
      lastSignature = "";
      poll();
    });

    document.getElementById("refreshBtn").addEventListener("click", () => {
      lastSignature = "";
      poll();
    });

    liveToggle.addEventListener("change", () => {
      dotEl.classList.toggle("paused", !liveToggle.checked);
      liveLabel.textContent = liveToggle.checked ? "실시간" : "일시정지";
    });
    preset.addEventListener("change", applyPreset);
    cMethod.addEventListener("change", writeDraft);
    cTool.addEventListener("change", writeDraft);
    [cValue, cFrom, cTo, clientName].forEach((input) => input.addEventListener("input", writeDraft));
    fillBtn.addEventListener("click", applyPreset);
    sendBtn.addEventListener("click", () => runSend(1));
    sendTwiceBtn.addEventListener("click", () => runSend(2));

    async function poll() {
      try {
        const res = await fetch("/inspect/log", { cache: "no-store" });
        const data = await res.json();
        const exchanges = data.exchanges || [];
        const signature = exchanges.length
          ? `${exchanges[0].seq}:${exchanges.length}:${hideBefore}:${Array.from(openRows).join(",")}`
          : `0:0:${hideBefore}`;
        if (signature !== lastSignature) {
          lastSignature = signature;
          render(exchanges);
        }
      } catch (e) {
        metaEl.textContent = "로그를 가져오지 못했습니다.";
      }
    }

    applyPreset();
    updateMetrics();
    setInterval(() => { if (liveToggle.checked) poll(); }, 1000);
    poll();
  </script>
</body>
</html>"""


def _advanced_field(
    path: str,
    kind: str,
    label: str,
    hint: str = "",
    options: Any = None,
) -> dict[str, Any]:
    return {"path": path, "kind": kind, "label": label, "hint": hint, "options": options}


def advanced_field_groups() -> tuple[tuple[str, str, list[dict[str, Any]]], ...]:
    """Controls of the /scenario "프로토콜 주입 설정" card, grouped by era.

    Each field is bound to a config path; the page reads and writes them
    generically (data-config-path / data-config-kind).
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


def render_advanced_controls() -> str:
    sections: list[str] = []
    for group_label, selection_rule, fields in advanced_field_groups():
        toggles: list[str] = []
        inputs: list[str] = []
        for field in fields:
            path = escape(field["path"])
            kind = field["kind"]
            hint = f'<span class="field-hint">{escape(field["hint"])}</span>' if field["hint"] else ""
            if kind == "bool":
                toggles.append(
                    f"""
        <label class="check-row" title="{escape(field["hint"])}">
          <input type="checkbox" data-config-path="{path}" data-config-kind="bool">
          <span>{escape(field["label"])}</span>
          <code>{path}</code>
        </label>
        """
                )
                continue
            if kind in {"select", "select-int"}:
                data_kind = "int" if kind == "select-int" else "string"
                options = "".join(
                    f'<option value="{escape(str(value))}">{escape(str(value))} · {escape(str(text))}</option>'
                    if str(value) != str(text)
                    else f'<option value="{escape(str(value))}">{escape(str(value))}</option>'
                    for value, text in field["options"].items()
                )
                control = (
                    f'<select data-config-path="{path}" data-config-kind="{data_kind}">{options}</select>'
                )
            else:
                input_type = "text" if kind == "list" else "number"
                step = ' step="any"' if kind == "float" else ""
                control = (
                    f'<input type="{input_type}"{step} autocomplete="off" '
                    f'data-config-path="{path}" data-config-kind="{kind}">'
                )
            inputs.append(
                f"""
        <label class="field">
          <span class="field-title">{escape(field["label"])} <code>{path}</code>{hint}</span>
          {control}
        </label>
        """
            )
        sections.append(
            f"""
        <section class="tool-error-section">
          <h3>{escape(group_label)} <span>{escape(selection_rule)}</span></h3>
          <div class="tool-error-list">
            {"".join(toggles)}
          </div>
          <div class="field-grid">
            {"".join(inputs)}
          </div>
        </section>
        """
        )
    return "\n".join(sections)


def render_scenario_page(message: str | None = None, error: str | None = None) -> str:
    active_scenario = get_active_scenario()
    active_config = get_active_config()
    active_title = SCENARIO_TITLES.get(active_scenario, "커스텀 설정")
    active_group = scenario_group_label(active_scenario) if active_scenario != "custom" else "커스텀"
    config_json = json.dumps(active_config, ensure_ascii=False)
    server_error_sections: list[str] = []
    for group_label, selection_rule, scenarios in SERVER_ERROR_GROUPS:
        controls = []
        for scenario in scenarios:
            conflict_group = ""
            if scenario in {"auth-401", "auth-403"}:
                conflict_group = "auth"
            if scenario in {
                "tools-list-error",
                "tools-list-null",
                "tools-list-empty",
                "too-many-tools",
            }:
                conflict_group = "tools-list"
            conflict_attr = (
                f' data-conflict="{escape(conflict_group)}"' if conflict_group else ""
            )
            # initialize/handshake-only scenarios don't exist in the stateless
            # 2026 transport, so hide them when the 2026 toggle is active.
            row_class = "check-row"
            if scenario in ERA_2025_ONLY_SCENARIOS:
                row_class += " era-2025-only"
            controls.append(
                f"""
        <label class="{row_class}">
          <input type="checkbox" name="serverErrors" value="{escape(scenario)}"{conflict_attr}>
          <span>{escape(SCENARIO_TITLES[scenario])}</span>
          <code>{escape(scenario)}</code>
        </label>
        """
            )
        server_error_sections.append(
            f"""
        <section class="tool-error-section">
          <h3>{escape(group_label)} <span>{escape(selection_rule)}</span></h3>
          <div class="tool-error-list">
            {"".join(controls)}
          </div>
        </section>
        """
        )
    server_error_controls = "\n".join(server_error_sections)
    tool_error_sections: list[str] = []
    for group_label, selection_rule, scenarios in TOOL_ERROR_GROUPS:
        controls = "\n".join(
            f"""
        <label class="check-row">
          <input type="checkbox" name="toolErrors" value="{escape(scenario)}">
          <span>{escape(SCENARIO_TITLES[scenario])}</span>
          <code>{escape(scenario)}</code>
        </label>
        """
            for scenario in scenarios
        )
        tool_error_sections.append(
            f"""
        <section class="tool-error-section">
          <h3>{escape(group_label)} <span>{escape(selection_rule)}</span></h3>
          <div class="tool-error-list">
            {controls}
          </div>
        </section>
        """
        )
    tool_error_controls = "\n".join(tool_error_sections)
    policy_reference_sections: list[str] = []
    for section_label, groups in (
        ("server 에러 시나리오", SERVER_ERROR_GROUPS),
        ("tools 에러 시나리오", TOOL_ERROR_GROUPS),
    ):
        group_cards = []
        for group_label, selection_rule, scenarios in groups:
            items = "\n".join(
                f"""
          <li>
            <div>
              <strong>{escape(SCENARIO_TITLES[scenario])}</strong>
              <code>{escape(scenario)}</code>
            </div>
            <p>{escape(SCENARIO_DESCRIPTIONS[scenario])}</p>
          </li>
          """
                for scenario in scenarios
            )
            group_cards.append(
                f"""
        <section class="policy-group">
          <h3>{escape(group_label)} <span>{escape(selection_rule)}</span></h3>
          <ul>
            {items}
          </ul>
        </section>
        """
            )
        policy_reference_sections.append(
            f"""
      <section class="policy-section">
        <h3>{escape(section_label)}</h3>
        <div class="policy-grid">
          {"".join(group_cards)}
        </div>
      </section>
      """
        )
    policy_reference = "\n".join(policy_reference_sections)
    advanced_controls = render_advanced_controls()
    notice = ""
    if message:
        notice = f'<div class="notice success">{escape(message)}</div>'
    if error:
        notice = f'<div class="notice error">{escape(error)}</div>'

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unit Expert MCP 상태 제어</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fa;
      --surface: #ffffff;
      --text: #151922;
      --muted: #5f6875;
      --line: #dfe3ea;
      --accent: #0f766e;
      --accent-strong: #0b5f59;
      --danger: #b42318;
      --success: #146c43;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    main {{
      width: min(1280px, calc(100vw - 32px));
      margin: 32px auto;
    }}
    header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }}
    .header-title {{
      min-width: 0;
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .endpoint {{
      color: var(--muted);
      font-size: 13px;
      background: #e9eef7;
      border: 1px solid #c8d1df;
      border-radius: 8px;
      padding: 14px;
      transition: background-color 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
    }}
    .endpoint.needs-apply {{
      background: #fff7ed;
      border-color: #fdba74;
    }}
    .endpoint.url-updated {{
      background: #ecfdf5;
      border-color: #86efac;
      box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.14);
      animation: urlPulse 900ms ease-out 1;
    }}
    @keyframes urlPulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.36); }}
      100% {{ box-shadow: 0 0 0 8px rgba(34, 197, 94, 0); }}
    }}
    .endpoint-label {{
      margin-bottom: 6px;
      color: var(--muted);
      font-weight: 600;
    }}
    .endpoint-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
    }}
    .endpoint-row code {{
      width: 100%;
      overflow-wrap: anywhere;
      background: #fff;
      border-color: #e1e6ef;
    }}
    .endpoint-hint {{
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .title-note {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 16px;
    }}
    .workspace {{
      display: grid;
      grid-template-columns: minmax(340px, 460px) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }}
    .workspace > * {{
      min-width: 0;
    }}
    .right-column {{
      display: grid;
      grid-column: 1;
      grid-row: 1;
      gap: 16px;
      min-width: 0;
      position: sticky;
      top: 16px;
      max-height: calc(100vh - 32px);
      overflow: auto;
      scrollbar-gutter: stable;
    }}
    .summary-sidebar {{
      display: grid;
      gap: 12px;
      background: #e9eef7;
      border: 1px solid #c8d1df;
      border-radius: 8px;
      padding: 16px;
    }}
    .summary-sidebar h2 {{
      margin: 0;
      font-size: 15px;
      letter-spacing: 0;
    }}
    .summary-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .summary-list {{
      display: grid;
      gap: 10px;
      margin: 0;
    }}
    .summary-item {{
      display: grid;
      gap: 4px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--line);
    }}
    .summary-item:last-child {{
      padding-bottom: 0;
      border-bottom: 0;
    }}
    .summary-item dt {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .summary-item dd {{
      margin: 0;
      font-size: 14px;
      color: var(--text);
      overflow-wrap: anywhere;
    }}
    .summary-item code {{
      width: fit-content;
      max-width: 100%;
    }}
    .summary-chip {{
      display: block;
      width: fit-content;
      max-width: 100%;
      margin-bottom: 6px;
      padding: 3px 7px;
      border: 1px solid #d8dee9;
      border-radius: 999px;
      background: #fff;
      color: var(--text);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .summary-chip:last-child {{
      margin-bottom: 0;
    }}
    .summary-lines {{
      display: grid;
      gap: 6px;
    }}
    .summary-line {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }}
    .summary-line-label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }}
    .left-column {{
      display: grid;
      grid-column: 2;
      grid-row: 1;
      gap: 16px;
      min-width: 0;
    }}
    .action-buttons {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
      min-width: 0;
    }}
    .spec-strip {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
    }}
    .spec-strip h2 {{
      margin: 0 0 12px;
      font-size: 15px;
      letter-spacing: 0;
    }}
    .server-settings-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }}
    .setting-block {{
      display: grid;
      gap: 7px;
    }}
    .setting-heading {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: baseline;
    }}
    .setting-heading strong {{
      font-size: 14px;
      font-weight: 700;
    }}
    .setting-heading span {{
      color: var(--muted);
      font-size: 12px;
    }}
    .summary {{
      min-width: 220px;
      font-size: 13px;
      color: var(--muted);
      display: none;
    }}
    .control-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }}
    .control-card {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .control-card.server-card {{
      background: #eef6ff;
    }}
    .server-card .tool-error-section {{
      background: #f8fbff;
      border-color: #cfe0f4;
    }}
    .control-card.tool-card {{
      background: #f6f2e9;
    }}
    .control-card.identity-card {{
      background: #f0f9f6;
      border-color: #b9ddd2;
    }}
    .tool-card .tool-error-section {{
      background: #fbf8ef;
      border-color: #ded6c3;
    }}
    .control-card.full {{
      grid-column: 1 / -1;
    }}
    .control-card h2 {{
      margin: 0 0 12px;
      font-size: 15px;
      letter-spacing: 0;
    }}
    .card-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }}
    .card-title h2 {{
      margin: 0;
    }}
    .step-badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 28px;
      height: 24px;
      border-radius: 999px;
      background: #e7f6f3;
      color: var(--accent-strong);
      font-size: 12px;
      font-weight: 700;
    }}
    .control-stack {{
      display: grid;
      gap: 9px;
    }}
    .field-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .field-grid.three {{
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }}
    .delay-grid {{
      grid-template-columns: minmax(180px, 240px) minmax(0, 1fr);
      align-items: end;
    }}
    .delay-grid .check-row {{
      min-height: 34px;
    }}
    .field {{
      display: grid;
      gap: 6px;
      min-width: 0;
      font-size: 14px;
    }}
    .field > span:first-child {{
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }}
    .field-title {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: baseline;
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }}
    .field-hint {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 400;
    }}
    .section-hint {{
      margin: -4px 0 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .sidebar-mcp-fields {{
      display: grid;
      gap: 14px;
      padding: 14px 0;
      border-top: 1px solid #c8d1df;
      border-bottom: 1px solid #c8d1df;
      margin-bottom: 14px;
    }}
    .sidebar-custom-header-row {{
      display: grid;
      gap: 8px;
    }}
    .sidebar-custom-header-label {{
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .custom-header-hint .hint-desc {{
      margin: 6px 0 0;
      color: #335c85;
      font-size: 12px;
      line-height: 1.6;
      grid-column: 1 / -1;
    }}
    .custom-header-card .card-title {{
      justify-content: space-between;
    }}
    .custom-header-toggle {{
      display: flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
    }}
    .custom-header-toggle input[type="checkbox"] {{
      position: absolute;
      opacity: 0;
      width: 0;
      height: 0;
    }}
    .toggle-track {{
      display: inline-flex;
      align-items: center;
      width: 40px;
      height: 22px;
      border-radius: 999px;
      background: var(--line);
      transition: background 0.2s;
      cursor: pointer;
      flex-shrink: 0;
    }}
    .toggle-thumb {{
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: #fff;
      margin-left: 3px;
      transition: transform 0.2s;
      box-shadow: 0 1px 3px rgba(0,0,0,0.2);
    }}
    .custom-header-toggle input:checked + .toggle-track {{
      background: var(--accent);
    }}
    .custom-header-toggle input:checked + .toggle-track .toggle-thumb {{
      transform: translateX(18px);
    }}
    .custom-header-hint {{
      display: grid;
      grid-template-columns: auto 1fr;
      align-items: center;
      gap: 6px 10px;
      margin-top: 4px;
      padding: 10px 14px;
      border-radius: 8px;
      background: #edf6ff;
      border: 1px solid #c2ddf7;
    }}
    .custom-header-hint .hint-label {{
      color: #335c85;
      font-size: 12px;
      font-weight: 600;
      white-space: nowrap;
    }}
    .custom-header-hint code {{
      font-family: monospace;
      font-size: 13px;
      background: #d6eaff;
      color: #1a4a7a;
      padding: 3px 8px;
      border-radius: 5px;
      letter-spacing: 0.02em;
    }}
    .check-grid {{
      display: grid;
      gap: 14px;
    }}
    .check-grid.two-column {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
      align-items: start;
    }}
    .tool-error-section {{
      display: grid;
      gap: 8px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f7f8fb;
    }}
    .tool-error-section h3 {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      margin: -4px -4px 2px;
      padding: 4px 6px;
      border-radius: 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .server-card .tool-error-section h3 {{
      background: #edf6ff;
      color: #335c85;
    }}
    .tool-card .tool-error-section h3 {{
      background: #f3ecdc;
      color: #6d5b36;
    }}
    .tool-error-section h3 span {{
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 0 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f5f6f8;
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
    }}
    .tool-error-list {{
      display: grid;
      gap: 6px;
    }}
    .policy-reference {{
      display: grid;
      gap: 18px;
    }}
    .policy-details {{
      padding: 0;
      overflow: hidden;
      background: #f7f8fb;
    }}
    .usage-details {{
      padding: 0;
      overflow: hidden;
      background: #eef7f4;
      border-color: #b8d9d0;
    }}
    .usage-details .usage-body {{
      display: grid;
      gap: 14px;
      padding: 16px;
      border-top: 1px solid #c7e2da;
    }}
    .usage-details .usage-steps {{
      margin: 0;
      padding-left: 20px;
      color: var(--text);
      font-size: 14px;
    }}
    .usage-details .usage-steps li + li {{
      margin-top: 7px;
    }}
    .usage-details .usage-note {{
      margin: 0;
      padding: 10px 11px;
      border: 1px solid #bdded4;
      border-radius: 6px;
      background: #f8fffc;
      color: #245a4c;
      font-size: 13px;
      line-height: 1.55;
    }}
    .usage-details .usage-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .usage-details .usage-box {{
      display: grid;
      gap: 6px;
      min-width: 0;
      padding: 10px 11px;
      border: 1px solid #d7e6e2;
      border-radius: 6px;
      background: #fff;
    }}
    .usage-details .usage-box strong {{
      font-size: 13px;
    }}
    .usage-details .usage-box p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .usage-toggle-label {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 28px;
      padding: 0 10px;
      border: 1px solid #aacfc5;
      border-radius: 999px;
      color: #245a4c;
      background: #fff;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .usage-toggle-label::before {{
      content: "펼쳐보기";
    }}
    .usage-details[open] .usage-toggle-label::before {{
      content: "접기";
    }}
    .policy-details summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 16px;
      cursor: pointer;
      list-style: none;
    }}
    .policy-details summary::-webkit-details-marker {{
      display: none;
    }}
    .policy-details summary::before {{
      content: "▸";
      color: var(--muted);
      font-size: 12px;
    }}
    .policy-details[open] summary::before {{
      content: "▾";
    }}
    .policy-details[open] summary {{
      border-bottom: 1px solid var(--line);
    }}
    .policy-details h2 {{
      margin: 0;
    }}
    .policy-summary-context {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
      text-align: right;
      overflow-wrap: anywhere;
    }}
    .policy-toggle-label {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 28px;
      padding: 0 10px;
      border: 1px solid #c7d0dd;
      border-radius: 999px;
      color: #475569;
      background: #fff;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .policy-toggle-label::before {{
      content: "펼쳐보기";
    }}
    .policy-details[open] .policy-toggle-label::before {{
      content: "접기";
    }}
    .policy-details .policy-reference {{
      padding: 16px;
    }}
    .policy-section {{
      display: grid;
      gap: 10px;
    }}
    .policy-section > h3 {{
      margin: 0;
      font-size: 14px;
      letter-spacing: 0;
    }}
    .policy-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-items: start;
    }}
    .policy-group {{
      display: grid;
      gap: 8px;
      min-width: 0;
    }}
    .policy-group h3 {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .policy-group h3 span {{
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 0 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f5f6f8;
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
    }}
    .policy-group ul {{
      display: grid;
      gap: 6px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .policy-group li {{
      display: grid;
      gap: 5px;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fafbfc;
    }}
    .policy-group li > div {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }}
    .policy-group strong {{
      font-size: 13px;
      font-weight: 700;
    }}
    .policy-group p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .radio-row, .check-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 30px;
      font-size: 14px;
    }}
    .check-grid .check-row {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      grid-template-areas:
        "input title"
        "input code";
      align-items: start;
      gap: 2px 8px;
      min-height: 58px;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
    }}
    .check-grid .check-row input {{
      grid-area: input;
      margin-top: 3px;
    }}
    .check-grid .check-row span {{
      grid-area: title;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .check-row code {{
      margin-left: auto;
      font-size: 11px;
      max-width: 48%;
      overflow-wrap: anywhere;
      white-space: normal;
    }}
    .check-grid .check-row code {{
      grid-area: code;
      width: fit-content;
      max-width: 100%;
      margin-left: 0;
    }}
    .check-grid.is-disabled .check-row {{
      opacity: 0.58;
      background: #f3f4f6;
    }}
    .inline-field {{
      display: grid;
      grid-template-columns: 160px minmax(0, 1fr);
      gap: 8px;
      align-items: center;
      font-size: 14px;
    }}
    input[type="number"], input[type="text"] {{
      width: 100%;
      height: 34px;
      border: 1px solid #cfd5df;
      border-radius: 6px;
      padding: 0 10px;
      font: inherit;
    }}
    select:disabled,
    input:disabled {{
      color: #98a2b3;
      background: #f3f4f6;
      cursor: not-allowed;
    }}
    .muted-note {{
      display: none;
      color: #8a4b0f;
      background: #fff7ed;
      border: 1px solid #fed7aa;
      border-radius: 6px;
      padding: 10px 11px;
      margin: -4px 0 12px;
      font-size: 13px;
    }}
    .muted-note.visible {{
      display: block;
    }}
    .status {{
      display: grid;
      grid-template-columns: 160px 1fr;
      gap: 8px 16px;
      margin-bottom: 16px;
      font-size: 14px;
    }}
    .status span:nth-child(odd) {{ color: var(--muted); }}
    code {{
      display: inline-block;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
      background: #f1f3f6;
      border: 1px solid #e4e7ec;
      border-radius: 4px;
      padding: 2px 5px;
    }}
    td strong {{
      display: block;
      margin-bottom: 5px;
      font-size: 14px;
    }}
    form {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto;
      gap: 10px;
      align-items: center;
    }}
    select, button {{
      height: 40px;
      font: inherit;
      border-radius: 6px;
    }}
    select {{
      width: 100%;
      border: 1px solid #cfd5df;
      background: #fff;
      color: var(--text);
      padding: 0 10px;
    }}
    button {{
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      padding: 0 14px;
      cursor: pointer;
      font-weight: 600;
    }}
    .copy-button {{
      height: 30px;
      padding: 0 10px;
      font-size: 13px;
      white-space: nowrap;
    }}
    button.secondary {{
      background: #fff;
      color: var(--accent-strong);
    }}
    .notice {{
      border-radius: 6px;
      padding: 10px 12px;
      margin-bottom: 14px;
      font-size: 14px;
      border: 1px solid;
    }}
    .notice.success {{
      color: var(--success);
      background: #edf8f2;
      border-color: #b7e2c8;
    }}
    .notice.error {{
      color: var(--danger);
      background: #fff0ed;
      border-color: #f2b8b0;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 14px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      text-align: left;
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      background: #f5f6f8;
      font-weight: 600;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    tr.active td {{ background: #ecfdf9; }}
    tr.section-row td {{
      background: #111827;
      color: #d1fae5;
      border-bottom-color: #111827;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .terminal-details {{
      background: #e9eef7;
      border: 1px solid #c8d1df;
      border-radius: 8px;
      overflow: hidden;
    }}
    .terminal-details summary {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 12px;
      padding: 12px;
      cursor: pointer;
      color: var(--text);
      font-size: 14px;
      font-weight: 700;
      background: #e9eef7;
    }}
    .terminal-details summary::-webkit-details-marker {{
      display: none;
    }}
    .terminal-details[open] summary {{
      border-bottom: 1px solid var(--line);
    }}
    .terminal-summary-label {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }}
    .terminal-summary-label::before {{
      content: "▸";
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 18px;
      height: 18px;
      border-radius: 999px;
      background: #e7ecf5;
      color: #475569;
      font-size: 11px;
      flex: 0 0 auto;
    }}
    .terminal-details[open] .terminal-summary-label::before {{
      content: "▾";
    }}
    .terminal-summary-context {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
      overflow-wrap: anywhere;
    }}
    .terminal-summary-text {{
      display: grid;
      gap: 3px;
      min-width: 0;
    }}
    .terminal-toggle-label {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 28px;
      padding: 0 10px;
      border: 1px solid #c7d0dd;
      border-radius: 999px;
      color: #475569;
      background: #fff;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .terminal-toggle-label::before {{
      content: "펼쳐보기";
    }}
    .terminal-details[open] .terminal-toggle-label::before {{
      content: "접기";
    }}
    .terminal {{
      background: #111827;
      color: #d1fae5;
      border: 0;
      border-radius: 0;
      min-height: 460px;
      overflow: hidden;
      min-width: 0;
    }}
    .terminal-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 10px 12px;
      color: #d1d5db;
      background: #0f172a;
      border-bottom: 1px solid #1f2937;
      font-size: 13px;
    }}
    .terminal pre {{
      margin: 0;
      padding: 14px;
      min-height: 414px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.55;
    }}
    @media (max-width: 1080px) {{
      header {{ display: block; }}
      .endpoint-row {{ grid-template-columns: 1fr; }}
      .action-buttons {{ justify-content: stretch; }}
      .status {{ grid-template-columns: 1fr; }}
      form {{ grid-template-columns: 1fr; }}
      button {{ width: 100%; }}
      .workspace {{ grid-template-columns: 1fr; }}
      .left-column, .right-column {{
        grid-column: auto;
        grid-row: auto;
        position: static;
        max-height: none;
        overflow: visible;
      }}
      .control-grid {{ grid-template-columns: 1fr; }}
      .server-settings-grid {{ grid-template-columns: 1fr; }}
      .field-grid, .field-grid.three {{ grid-template-columns: 1fr; }}
      .check-grid.two-column {{ grid-template-columns: 1fr; }}
      .policy-grid {{ grid-template-columns: 1fr; }}
      .usage-details .usage-grid {{ grid-template-columns: 1fr; }}
      .inline-field {{ grid-template-columns: 1fr; }}
      .terminal {{ min-height: 360px; }}
      .terminal pre {{ min-height: 314px; }}
    }}
    .era-toggle {{
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 6px;
      flex-shrink: 0;
    }}
    .era-toggle-label {{
      font-size: 12px;
      font-weight: 600;
      color: var(--muted);
      letter-spacing: 0.02em;
    }}
    .era-toggle-buttons {{
      display: inline-flex;
      background: #e9eef7;
      border: 1px solid #c8d1df;
      border-radius: 10px;
      padding: 3px;
      gap: 3px;
    }}
    .era-button {{
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--muted);
      font-size: 14px;
      font-weight: 700;
      padding: 6px 18px;
      border-radius: 8px;
      cursor: pointer;
      transition: background-color 140ms ease, color 140ms ease, box-shadow 140ms ease;
    }}
    .era-button.is-active {{
      background: var(--accent);
      color: #ffffff;
      box-shadow: 0 1px 2px rgba(15, 118, 110, 0.35);
    }}
    .era-toggle-hint {{
      margin: 0;
      font-size: 12px;
      color: var(--muted);
      max-width: 260px;
      text-align: right;
    }}
    body[data-era="2026"] .era-2025-only {{
      display: none !important;
    }}
    /* 2026 전용 컨트롤은 2025 탭에서 숨긴다. */
    body:not([data-era="2026"]) .era-2026-only {{
      display: none !important;
    }}
    body[data-era="2026"] .era-2026-badge {{
      display: inline-flex;
    }}
    .era-2026-badge {{
      display: none;
      margin-left: 8px;
      font-size: 11px;
      font-weight: 700;
      color: var(--accent-strong);
      background: #d7f2ee;
      border-radius: 6px;
      padding: 2px 8px;
      vertical-align: middle;
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="header-title">
        <h1>Unit Expert MCP 상태 제어</h1>
        <div class="title-note">
          MCP 상태는 서버 전역으로 공유됩니다. 여러 사용자가 동시에 적용하면 마지막 적용값으로 바뀔 수 있습니다.
        </div>
      </div>
      <div class="era-toggle" role="group" aria-label="프로토콜 버전 선택">
        <span class="era-toggle-label">프로토콜 버전</span>
        <div class="era-toggle-buttons">
          <button type="button" class="era-button" data-era="2025">2025</button>
          <button type="button" class="era-button" data-era="2026">2026</button>
        </div>
        <p class="era-toggle-hint" id="eraHint"></p>
      </div>
    </header>
    <div class="workspace">
      <div class="left-column">
        {notice}
        <span id="activeTitle" hidden>{escape(active_title)}</span>
        <span id="activeGroup" hidden>{escape(active_group)}</span>
        <span id="activeScenario" hidden>{escape(active_scenario)}</span>
        <div class="control-grid">
          <details class="control-card full policy-details usage-details">
            <summary>
              <h2>사용 설명서</h2>
              <span class="usage-toggle-label" aria-hidden="true"></span>
            </summary>
            <div class="usage-body">
              <ol class="usage-steps">
                <li><strong>MCP 상태 제어 페이지</strong>에 접속합니다.</li>
                <li>테스트할 MCP의 <strong>MCP 식별자</strong>와 <strong>MCP 이름(서비스 이름)</strong>으로 변경합니다. 필요한 경우 <strong>커스텀 헤더 설정 여부</strong>도 설정합니다.</li>
                <li>원하는 시나리오를 선택한 뒤 <strong>적용</strong> 버튼을 누릅니다.</li>
                <li>좌측 상단의 <strong>테스트용 MCP URL</strong> 옆 <strong>복사</strong> 버튼을 누릅니다.</li>
                <li>복사된 URL을 MCP 등록/수정 요청의 <strong>endpoint URL</strong> 값에 넣고 검증합니다.</li>
              </ol>
              <p class="usage-note">
                적용 버튼은 서버 상태를 바꾸는 것이 아니라, 현재 설정이 포함된 테스트용 MCP URL을 생성합니다.
              </p>
            </div>
          </details>
          <section class="control-card full server-card">
            <div class="card-title">
              <h2>server 에러 시나리오</h2>
              <span class="step-badge">1</span>
            </div>
            <p class="section-hint">
              MCP 연결, initialize, tools/list 단계에서 발생시키고 싶은 server 응답을 선택합니다.
            </p>
            <div class="check-grid two-column" id="serverErrorGrid">
              {server_error_controls}
            </div>
          </section>
          <section class="control-card full tool-card">
            <div class="card-title">
              <h2>tools 에러 시나리오</h2>
              <span class="step-badge">2</span>
            </div>
            <div id="toolErrorDisabledNote" class="muted-note">
              tools/list 응답 시나리오가 먼저 적용됩니다. tools 에러 시나리오를 사용하려면 server 에러 시나리오의 tools/list 응답 선택을 해제해 주세요.
            </div>
            <div class="check-grid two-column" id="toolErrorGrid">
              {tool_error_controls}
            </div>
          </section>
          <section class="control-card full server-card" id="advancedCard">
            <div class="card-title">
              <h2>프로토콜 주입 설정</h2>
              <span class="step-badge">3</span>
            </div>
            <p class="section-hint">
              2026-07-28 스펙 준수 여부, server/discover·tools/call 에러 주입, 레거시 세션·버전·페이지네이션을 설정합니다. 전체 옵션은 docs/CONFIG_REFERENCE.md를 참고하세요.
            </p>
            <div class="check-grid" id="advancedGrid">
              {advanced_controls}
            </div>
            <p class="section-hint">
              <button id="expireSessions" class="secondary" type="button">발급된 세션 전부 만료</button>
              <span id="expireSessionsResult"></span>
              session.validate가 켜진 URL에서 다음 요청이 404가 됩니다(재연결 테스트).
            </p>
          </section>
          <details class="control-card full policy-details">
            <summary>
              <h2>시나리오별 설명</h2>
              <span class="policy-toggle-label" aria-hidden="true"></span>
            </summary>
            <div class="policy-reference" id="scenarioRows">
              {policy_reference}
            </div>
          </details>
        </div>
      </div>
      <aside class="right-column">
        <section class="endpoint" aria-label="MCP URL">
          <div class="endpoint-label">테스트용 MCP URL</div>
          <div class="endpoint-row">
            <code id="mcpUrl">{escape(PUBLIC_MCP_URL)}</code>
            <button id="copyMcpUrl" class="copy-button" type="button">복사</button>
          </div>
          <p id="mcpUrlHint" class="endpoint-hint">설정을 바꾼 뒤 적용을 누르면 이 URL이 갱신됩니다.</p>
        </section>
        <section class="summary-sidebar" aria-label="현재 MCP 설정 요약">
          <div class="summary-header">
            <h2>MCP 설정</h2>
            <div class="action-buttons">
              <button id="applyConfig" type="button">적용</button>
              <button id="resetConfig" class="secondary" type="button">초기화</button>
            </div>
          </div>
          <div class="sidebar-mcp-fields">
            <div class="field-grid">
              <label class="field">
                <span class="field-title">
                  MCP 식별자
                  <span class="field-hint">initialize의 serverInfo.name에 반환됩니다.</span>
                </span>
                <input id="mcpIdentifier" type="text" maxlength="128" autocomplete="off">
              </label>
              <label class="field">
                <span class="field-title">
                  MCP 이름(서비스 이름)
                  <span class="field-hint">tool description에 포함됩니다.</span>
                </span>
                <input id="mcpServiceName" type="text" maxlength="200" autocomplete="off">
              </label>
            </div>
            <div class="sidebar-custom-header-row">
              <div class="sidebar-custom-header-label">
                <span class="field-title">커스텀 헤더 설정 여부</span>
                <label class="custom-header-toggle">
                  <input type="checkbox" id="customHeaderEnabled">
                  <span class="toggle-track"><span class="toggle-thumb"></span></span>
                </label>
              </div>
              <div id="customHeaderHint" class="custom-header-hint" hidden>
                <span class="hint-label">요청 헤더</span>
                <code>X-Mock-Auth: allow</code>
                <p class="hint-desc">에러 시나리오와 별개로 동작합니다. 헤더가 없으면 401, 값이 틀리면 403을 반환합니다.</p>
              </div>
            </div>
            <div class="sidebar-custom-header-row" id="authModeRow">
              <div class="sidebar-custom-header-label">
                <span class="field-title">
                  OAuth 인증 모드
                  <span class="field-hint">off=인증 없음, oauth=전체 보호, mixed-oauth=일부 툴만 보호(비로그인도 나머지 사용).</span>
                </span>
                <select id="authMode">
                  <option value="off">off · 인증 없음</option>
                  <option value="oauth">oauth · 전체 보호</option>
                  <option value="mixed-oauth">mixed-oauth · 일부 툴만</option>
                </select>
              </div>
              <div id="authModeHint" class="custom-header-hint" hidden>
                <span class="hint-label">동작</span>
                <code>401 + WWW-Authenticate: Bearer resource_metadata="…"</code>
                <p class="hint-desc">자체 미니 AS(/authorize·/token·/register)가 토큰을 발급합니다. oauth는 모든 요청, mixed-oauth는 보호 툴 tools/call만 401을 반환합니다.</p>
              </div>
            </div>
            <div class="sidebar-custom-header-row" id="serverEraRow">
              <div class="sidebar-custom-header-label">
                <span class="field-title">
                  서버 세대 (serverEra)
                  <span class="field-hint">both=2026·레거시 모두 처리, legacy=2026을 모르는 레거시 서버처럼 동작, modern=2026만 지원(레거시 요청 -32022).</span>
                </span>
                <select id="serverEra">
                  <option value="both">both · 둘 다</option>
                  <option value="legacy">legacy · 레거시 전용</option>
                  <option value="modern">modern · 2026 전용</option>
                </select>
              </div>
              <div id="serverEraHint" class="custom-header-hint" hidden>
                <span class="hint-label">동작</span>
                <code id="serverEraHintCode"></code>
                <p class="hint-desc" id="serverEraHintDesc"></p>
              </div>
            </div>
            <div class="sidebar-custom-header-row era-2026-only" id="rejectLegacyRow">
              <div class="sidebar-custom-header-label">
                <span class="field-title">
                  지원 프로토콜 버전 2026-07-28로 고정
                  <span class="field-hint">이 버전만 지원합니다. 구버전(2025 이하) initialize 핸드셰이크 요청은 -32022로 거부됩니다.</span>
                </span>
                <label class="custom-header-toggle">
                  <input type="checkbox" id="rejectLegacyEnabled">
                  <span class="toggle-track"><span class="toggle-thumb"></span></span>
                </label>
              </div>
              <div id="rejectLegacyHint" class="custom-header-hint" hidden>
                <span class="hint-label">동작</span>
                <code>-32022 UnsupportedProtocolVersion</code>
                <p class="hint-desc">2026 전송 마커(_meta·Mcp-Method)가 없는 요청은 모두 거부됩니다. 최신 버전을 아직 지원하지 않는 클라이언트가 실제로 실패하는지 확인할 때 사용합니다.</p>
              </div>
            </div>
            <div class="sidebar-custom-header-row era-2026-only" id="toolsListCacheRow">
              <div class="sidebar-custom-header-label">
                <span class="field-title">
                  tools/list 캐시 힌트
                  <span class="field-hint">2026 tools/list 응답에 ttlMs 60000을 내려 클라이언트 캐시 동작을 확인합니다.</span>
                </span>
                <label class="custom-header-toggle">
                  <input type="checkbox" id="toolsListCacheEnabled">
                  <span class="toggle-track"><span class="toggle-thumb"></span></span>
                </label>
              </div>
              <div id="toolsListCacheHint" class="custom-header-hint" hidden>
                <span class="hint-label">응답 필드</span>
                <code>ttlMs: 60000, cacheScope: private</code>
                <p class="hint-desc">같은 클라이언트 인스턴스가 60초 안에 tools/list를 다시 호출하면 SDK 캐시에서 반환될 수 있습니다.</p>
              </div>
            </div>
          </div>
          <dl class="summary-list">
            <div class="summary-item">
              <dt>server 에러 시나리오</dt>
              <dd id="summaryServerScenarios">-</dd>
            </div>
            <div class="summary-item">
              <dt>tools 에러 시나리오</dt>
              <dd id="summaryToolScenarios">-</dd>
            </div>
          </dl>
        </section>
        <details class="terminal-details">
          <summary>
            <span class="terminal-summary-text">
              <span class="terminal-summary-label">실제 응답 미리보기</span>
              <span class="terminal-summary-context">MCP 요청과 반환 응답을 JSON으로 확인합니다.</span>
            </span>
            <span class="terminal-toggle-label" aria-hidden="true"></span>
          </summary>
          <section class="terminal" aria-label="MCP 응답 미리보기">
            <pre id="responsePreview">Loading...</pre>
          </section>
        </details>
      </aside>
    </div>
  </main>
  <script>
    const initialConfig = {config_json};
    const defaultConfig = {json.dumps(DEFAULT_CONFIG, ensure_ascii=False)};
    const scenarioTitles = {json.dumps(SCENARIO_TITLES, ensure_ascii=False)};
    const activeScenario = document.getElementById("activeScenario");
    const activeGroup = document.getElementById("activeGroup");
    const activeTitle = document.getElementById("activeTitle");
    const preview = document.getElementById("responsePreview");
    const copyMcpUrl = document.getElementById("copyMcpUrl");
    const mcpUrl = document.getElementById("mcpUrl");
    const endpointCard = document.querySelector(".endpoint");
    const mcpUrlHint = document.getElementById("mcpUrlHint");
    const applyConfig = document.getElementById("applyConfig");
    const resetConfig = document.getElementById("resetConfig");
    const toolErrorDisabledNote = document.getElementById("toolErrorDisabledNote");
    const toolErrorGrid = document.getElementById("toolErrorGrid");
    const summaryServerScenarios = document.getElementById("summaryServerScenarios");
    const summaryToolScenarios = document.getElementById("summaryToolScenarios");
    const mcpIdentifier = document.getElementById("mcpIdentifier");
    const mcpServiceName = document.getElementById("mcpServiceName");
    const publicMcpUrl = {json.dumps(PUBLIC_MCP_URL)};
    const configQueryParam = {json.dumps(CONFIG_QUERY_PARAM)};
    const protocolVersion2026 = {json.dumps(PROTOCOL_VERSION_2026)};
    const metaProtocolVersionKey = {json.dumps(META_PROTOCOL_VERSION)};
    const metaClientCapabilitiesKey = {json.dumps(META_CLIENT_CAPABILITIES)};
    const metaClientInfoKey = {json.dumps(META_CLIENT_INFO)};
    const eraButtons = Array.from(document.querySelectorAll(".era-button"));
    const eraHint = document.getElementById("eraHint");
    const eraHints = {{
      "2025": "initialize 핸드셰이크 + 세션ID를 쓰는 기존 방식입니다.",
      "2026": "핸드셰이크 없이 요청마다 _meta·헤더를 싣는 stateless 방식입니다."
    }};
    let appliedConfig = clone(initialConfig);
    let currentEra = appliedConfig.protocolEra === "2026" ? "2026" : "2025";

    function clone(value) {{
      return JSON.parse(JSON.stringify(value));
    }}

    function setButtonBusy(button, label) {{
      button.textContent = label;
      button.disabled = true;
    }}

    function flashButton(button, label, resetLabel) {{
      button.textContent = label;
      button.disabled = false;
      window.setTimeout(() => {{
        button.textContent = resetLabel;
      }}, 1100);
    }}

    function pretty(value) {{
      try {{
        return JSON.stringify(value, null, 2);
      }} catch (error) {{
        return String(value);
      }}
    }}

    function compactConfig(config) {{
      function diff(value, defaultValue) {{
        if (
          value &&
          defaultValue &&
          typeof value === "object" &&
          typeof defaultValue === "object" &&
          !Array.isArray(value) &&
          !Array.isArray(defaultValue)
        ) {{
          const result = {{}};
          Object.entries(value).forEach(([key, childValue]) => {{
            const childDiff = diff(childValue, defaultValue[key]);
            if (childDiff !== undefined) result[key] = childDiff;
          }});
          return Object.keys(result).length ? result : undefined;
        }}
        return JSON.stringify(value) === JSON.stringify(defaultValue) ? undefined : value;
      }}
      return diff(config, defaultConfig) || {{}};
    }}

    function encodeConfig(config) {{
      const compact = compactConfig(config);
      if (!Object.keys(compact).length) return "";
      const json = JSON.stringify(compact);
      const bytes = new TextEncoder().encode(json);
      let binary = "";
      bytes.forEach((byte) => {{
        binary += String.fromCharCode(byte);
      }});
      return btoa(binary)
        .replace(/\\+/g, "-")
        .replace(/\\//g, "_")
        .replace(/=+$/g, "");
    }}

    function buildMcpUrl(config) {{
      const url = new URL(publicMcpUrl);
      const token = encodeConfig(config);
      if (token) {{
        url.searchParams.set(configQueryParam, token);
      }} else {{
        url.searchParams.delete(configQueryParam);
      }}
      return url.toString();
    }}

    function buildLocalMcpPath(config) {{
      const token = encodeConfig(config);
      return token ? `/mcp?${{configQueryParam}}=${{encodeURIComponent(token)}}` : "/mcp";
    }}

    function markUrlNeedsApply() {{
      endpointCard.classList.remove("url-updated");
      endpointCard.classList.add("needs-apply");
      mcpUrlHint.textContent = "선택값이 바뀌었습니다. 적용을 누르면 테스트용 URL이 갱신됩니다.";
      copyMcpUrl.textContent = "복사";
    }}

    function setGeneratedUrl(config, highlight) {{
      mcpUrl.textContent = buildMcpUrl(config);
      endpointCard.classList.remove("needs-apply");
      if (highlight) {{
        endpointCard.classList.remove("url-updated");
        void endpointCard.offsetWidth;
        endpointCard.classList.add("url-updated");
        mcpUrlHint.textContent = "URL이 갱신됐습니다. 이 URL을 복사해서 FE/Swagger내 Endpoint URL 필드에 넣어주세요.";
        copyMcpUrl.textContent = "이 URL 복사";
      }} else {{
        endpointCard.classList.remove("url-updated");
        mcpUrlHint.textContent = "이 URL은 현재 설정을 포함합니다. 복사해서 FE/Swagger에 넣으면 같은 응답을 반환합니다.";
      }}
    }}

    function escapeHtml(value) {{
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }}

    function formatHttp(label, response) {{
      const body = typeof response.body === "string" ? response.body : pretty(response.body);
      return [
        `$ ${{label}}`,
        `HTTP ${{response.status}}`,
        `content-type: ${{response.contentType || "-"}}`,
        "",
        body
      ].join("\\n");
    }}

    function selectedToolErrors() {{
      return Array.from(document.querySelectorAll('input[name="toolErrors"]:checked'))
        .map((input) => input.value);
    }}

    function selectedServerErrors() {{
      return Array.from(document.querySelectorAll('input[name="serverErrors"]:checked'))
        .map((input) => input.value);
    }}

    function toolsListModeFromServerErrors(serverErrors) {{
      if (serverErrors.includes("tools-list-error")) return "json-rpc-error";
      if (serverErrors.includes("tools-list-null")) return "null";
      if (serverErrors.includes("tools-list-empty")) return "empty";
      if (serverErrors.includes("too-many-tools")) return "too-many";
      return "normal";
    }}

    function serverErrorsFromConfig(config) {{
      const scenarios = [];
      if (config.server.httpStatus === 401) scenarios.push("auth-401");
      if (config.server.httpStatus === 403) scenarios.push("auth-403");
      if (config.initialize.protocolVersion === "2024-11-05") {{
        scenarios.push("unsupported-min-version");
      }}
      if (config.server.delayEnabled) scenarios.push("delayed-response");
      const modeToScenario = {{
        "json-rpc-error": "tools-list-error",
        "null": "tools-list-null",
        "empty": "tools-list-empty",
        "too-many": "too-many-tools"
      }};
      if (modeToScenario[config.toolsList.mode]) {{
        scenarios.push(modeToScenario[config.toolsList.mode]);
      }}
      if (config.toolsList.duplicateToolName) scenarios.push("duplicate-tool-name");
      return scenarios;
    }}

    function getPath(source, path) {{
      return path.split(".").reduce(
        (current, key) => (current === undefined || current === null ? undefined : current[key]),
        source
      );
    }}

    function setPath(target, path, value) {{
      const keys = path.split(".");
      let current = target;
      keys.slice(0, -1).forEach((key) => {{
        if (typeof current[key] !== "object" || current[key] === null) current[key] = {{}};
        current = current[key];
      }});
      current[keys[keys.length - 1]] = value;
    }}

    function advancedInputs() {{
      return Array.from(document.querySelectorAll("[data-config-path]"));
    }}

    function readAdvanced(input) {{
      const kind = input.dataset.configKind;
      const fallback = getPath(defaultConfig, input.dataset.configPath);
      if (kind === "bool") return input.checked;
      if (kind === "int") {{
        const parsed = parseInt(input.value, 10);
        return Number.isFinite(parsed) ? parsed : fallback;
      }}
      if (kind === "float") {{
        const parsed = parseFloat(input.value);
        return Number.isFinite(parsed) ? parsed : fallback;
      }}
      if (kind === "list") {{
        return input.value.split(",").map((item) => item.trim()).filter(Boolean);
      }}
      return input.value || fallback;
    }}

    function writeAdvanced(input, config) {{
      let value = getPath(config, input.dataset.configPath);
      if (value === undefined) value = getPath(defaultConfig, input.dataset.configPath);
      const kind = input.dataset.configKind;
      if (kind === "bool") {{
        input.checked = !!value;
      }} else if (kind === "list") {{
        input.value = (value || []).join(", ");
      }} else {{
        input.value = String(value);
      }}
    }}

    function setControls(config) {{
      advancedInputs().forEach((input) => writeAdvanced(input, config));
      mcpIdentifier.value = config.mcp.identifier;
      mcpServiceName.value = config.mcp.serviceName;
      const serverErrors = serverErrorsFromConfig(config);
      document.querySelectorAll('input[name="serverErrors"]').forEach((input) => {{
        input.checked = serverErrors.includes(input.value);
      }});
      document.querySelectorAll('input[name="toolErrors"]').forEach((input) => {{
        input.checked = config.toolErrors.includes(input.value);
      }});
      const customHeaderEl = document.getElementById("customHeaderEnabled");
      if (customHeaderEl) {{
        customHeaderEl.checked = !!(config.customHeader && config.customHeader.enabled);
        syncCustomHeaderHint();
      }}
      const authModeEl = document.getElementById("authMode");
      if (authModeEl) {{
        authModeEl.value = (config.auth && config.auth.mode) || "off";
        syncAuthModeHint();
      }}
      const serverEraEl = document.getElementById("serverEra");
      if (serverEraEl) {{
        serverEraEl.value = config.serverEra || (config.rejectLegacy ? "modern" : "both");
        syncServerEraHint();
      }}
      const rejectLegacyEl = document.getElementById("rejectLegacyEnabled");
      if (rejectLegacyEl) {{
        rejectLegacyEl.checked = !!config.rejectLegacy;
        syncRejectLegacyHint();
      }}
      const toolsListCacheEl = document.getElementById("toolsListCacheEnabled");
      if (toolsListCacheEl) {{
        toolsListCacheEl.checked = !!(config.toolsList && config.toolsList.cacheTtlMs > 0);
        syncToolsListCacheHint();
      }}
      syncEnabledStates(config);
      updateSummary(config, false);
    }}

    function collectConfig() {{
      const serverErrors = selectedServerErrors();
      const mode = toolsListModeFromServerErrors(serverErrors);
      const duplicateToolName =
        ["normal", "too-many"].includes(mode) && serverErrors.includes("duplicate-tool-name");
      const toolErrors = mode === "normal" ? selectedToolErrors() : [];
      const config = {{
        protocolEra: currentEra,
        rejectLegacy: currentEra === "2026" && document.getElementById("rejectLegacyEnabled").checked,
        serverEra: document.getElementById("serverEra").value,
        mcp: {{
          identifier: mcpIdentifier.value.trim() || defaultConfig.mcp.identifier,
          serviceName: mcpServiceName.value.trim() || defaultConfig.mcp.serviceName
        }},
        server: {{
          httpStatus: serverErrors.includes("auth-403")
            ? 403
            : serverErrors.includes("auth-401")
              ? 401
              : 200,
          target: "all",
          delayEnabled: serverErrors.includes("delayed-response"),
          delaySeconds: 5
        }},
        initialize: {{
          protocolVersionEnabled: true,
          protocolVersion: serverErrors.includes("unsupported-min-version")
            ? "2024-11-05"
            : "2025-03-26"
        }},
        toolsList: {{
          mode,
          tooManyCount: 21,
          duplicateToolName,
          cacheTtlMs: currentEra === "2026" && document.getElementById("toolsListCacheEnabled").checked
            ? 60000
            : 0,
          cacheScope: "private"
        }},
        customHeader: {{
          enabled: document.getElementById("customHeaderEnabled").checked,
          name: "X-Mock-Auth",
          value: "allow"
        }},
        auth: {{
          mode: document.getElementById("authMode").value
        }},
        toolErrors
      }};
      advancedInputs().forEach((input) => {{
        setPath(config, input.dataset.configPath, readAdvanced(input));
      }});
      // The "최소 지원 버전" scenario checkbox outranks the version select.
      if (serverErrors.includes("unsupported-min-version")) {{
        config.initialize.protocolVersion = "2024-11-05";
      }}
      return config;
    }}

    function syncCustomHeaderHint() {{
      const enabled = document.getElementById("customHeaderEnabled").checked;
      document.getElementById("customHeaderHint").hidden = !enabled;
    }}

    document.getElementById("customHeaderEnabled").addEventListener("change", () => {{
      syncCustomHeaderHint();
      handleControlChange();
    }});

    function syncAuthModeHint() {{
      const mode = document.getElementById("authMode").value;
      document.getElementById("authModeHint").hidden = mode === "off";
    }}

    document.getElementById("authMode").addEventListener("change", () => {{
      syncAuthModeHint();
      handleControlChange();
    }});

    function syncRejectLegacyHint() {{
      const enabled = document.getElementById("rejectLegacyEnabled").checked;
      document.getElementById("rejectLegacyHint").hidden = !enabled;
    }}

    const serverEraHints = {{
      legacy: [
        "server/discover → -32601 Method not found",
        "Mcp-Method·_meta 등 2026 표식을 무시하고 모든 요청을 레거시(initialize 핸드셰이크) 경로로 처리합니다. modern client → legacy server 조합을 확인할 때 사용합니다."
      ],
      modern: [
        "-32022 UnsupportedProtocolVersion",
        "2026 전송 표식이 없는 레거시 요청을 거부합니다. '지원 프로토콜 버전 2026-07-28로 고정'(rejectLegacy)과 같은 동작입니다."
      ]
    }};

    function syncServerEraHint() {{
      const value = document.getElementById("serverEra").value;
      const hint = serverEraHints[value];
      document.getElementById("serverEraHint").hidden = !hint;
      document.getElementById("serverEraHintCode").textContent = hint ? hint[0] : "";
      document.getElementById("serverEraHintDesc").textContent = hint ? hint[1] : "";
    }}

    // serverEra is the source of truth; the rejectLegacy toggle is its alias for
    // "modern", so the two controls are kept in step.
    document.getElementById("serverEra").addEventListener("change", () => {{
      document.getElementById("rejectLegacyEnabled").checked =
        currentEra === "2026" && document.getElementById("serverEra").value === "modern";
      syncRejectLegacyHint();
      syncServerEraHint();
      handleControlChange();
    }});

    document.getElementById("rejectLegacyEnabled").addEventListener("change", () => {{
      const serverEraSelect = document.getElementById("serverEra");
      if (document.getElementById("rejectLegacyEnabled").checked) {{
        serverEraSelect.value = "modern";
      }} else if (serverEraSelect.value === "modern") {{
        serverEraSelect.value = "both";
      }}
      syncServerEraHint();
      syncRejectLegacyHint();
      handleControlChange();
    }});

    function syncToolsListCacheHint() {{
      const enabled = document.getElementById("toolsListCacheEnabled").checked;
      document.getElementById("toolsListCacheHint").hidden = !enabled;
    }}

    document.getElementById("toolsListCacheEnabled").addEventListener("change", () => {{
      syncToolsListCacheHint();
      handleControlChange();
    }});

    function syncEnabledStates(config) {{
      const toolErrorsEnabled = config.toolsList.mode === "normal";
      const duplicateToolEnabled = ["normal", "too-many"].includes(config.toolsList.mode);
      toolErrorDisabledNote.classList.toggle("visible", !toolErrorsEnabled);
      toolErrorGrid.classList.toggle("is-disabled", !toolErrorsEnabled);
      document.querySelectorAll('input[name="toolErrors"]').forEach((input) => {{
        input.disabled = !toolErrorsEnabled;
        if (!toolErrorsEnabled) {{
          input.checked = false;
        }}
      }});
      const duplicateToolInput = document.querySelector(
        'input[name="serverErrors"][value="duplicate-tool-name"]'
      );
      if (duplicateToolInput) {{
        duplicateToolInput.disabled = !duplicateToolEnabled;
        if (!duplicateToolEnabled) {{
          duplicateToolInput.checked = false;
        }}
      }}
    }}

    function enforceExclusiveScenario(input) {{
      if (!input.checked || !input.dataset.conflict) return;
      document
        .querySelectorAll(`input[name="serverErrors"][data-conflict="${{input.dataset.conflict}}"]`)
        .forEach((candidate) => {{
          if (candidate !== input) candidate.checked = false;
        }});
    }}

    function titleForToolError(error) {{
      return scenarioTitles[error] || error;
    }}

    function renderScenarioSummary(element, scenarios) {{
      if (!scenarios.length) {{
        element.textContent = "없음";
        return;
      }}
      element.innerHTML = scenarios
        .map((scenario) => `<span class="summary-chip">${{scenarioTitles[scenario] || scenario}}</span>`)
        .join("");
    }}

    function summarizeConfig(config) {{
      const parts = [];
      if (config.server.httpStatus !== 200) {{
        parts.push(`HTTP ${{config.server.httpStatus}}`);
      }}
      if (config.server.delayEnabled) {{
        parts.push(`응답 지연 ${{config.server.delaySeconds}}초`);
      }}
      if (config.initialize.protocolVersion !== "2025-03-26") {{
        parts.push(`protocolVersion ${{config.initialize.protocolVersion}}`);
      }}
      if (config.toolsList.mode !== "normal") {{
        const labels = {{
          "json-rpc-error": "tools/list JSON-RPC error",
          "null": "tools: null",
          "empty": "tools: []",
          "too-many": `tools ${{config.toolsList.tooManyCount}}개`
        }};
        parts.push(labels[config.toolsList.mode] || config.toolsList.mode);
      }}
      if (config.toolsList.duplicateToolName) {{
        parts.push(titleForToolError("duplicate-tool-name"));
      }}
      if (config.toolsList.cacheTtlMs > 0) {{
        parts.push(`tools/list cache ${{config.toolsList.cacheTtlMs}}ms`);
      }}
      for (const error of config.toolErrors) {{
        parts.push(titleForToolError(error));
      }}
      advancedInputs().forEach((input) => {{
        const path = input.dataset.configPath;
        if (path === "initialize.protocolVersion") return;
        const value = getPath(config, path);
        const fallback = getPath(defaultConfig, path);
        if (value !== undefined && JSON.stringify(value) !== JSON.stringify(fallback)) {{
          parts.push(`${{path}}=${{Array.isArray(value) ? value.join("|") : value}}`);
        }}
      }});
      return parts.length ? parts.join(" + ") : "정상 응답";
    }}

    function updateConfigSummary(config) {{
      renderScenarioSummary(summaryServerScenarios, serverErrorsFromConfig(config));
      renderScenarioSummary(
        summaryToolScenarios,
        config.toolsList.mode === "normal" ? config.toolErrors : []
      );
    }}

    function updateSummary(config, applied) {{
      const hasServerError =
        config.server.httpStatus !== 200 ||
        config.server.delayEnabled ||
        config.initialize.protocolVersion !== "2025-03-26" ||
        config.toolsList.mode !== "normal" ||
        config.toolsList.duplicateToolName;
      const hasToolError = config.toolErrors.length > 0;
      const group = hasServerError && hasToolError
        ? "서버 error + tool error"
        : hasServerError
          ? "서버 error"
          : hasToolError
            ? "tool error"
            : "기본";
      const title = summarizeConfig(config);
      activeScenario.textContent = applied ? "custom" : "custom · 미적용";
      activeGroup.textContent = group;
      activeTitle.textContent = title;
      updateConfigSummary(config);
    }}

    async function postMcp(payload, config, extraHeaders) {{
      const era = (config && config.protocolEra === "2026") ? "2026" : "2025";
      const headers = {{ "Content-Type": "application/json" }};
      if (era === "2026") {{
        // Stateless transport: single JSON response, per-request routing headers.
        headers["Accept"] = "application/json";
        headers["MCP-Protocol-Version"] = protocolVersion2026;
        headers["Mcp-Method"] = payload.method;
        if (payload.params && typeof payload.params.name === "string") {{
          headers["Mcp-Name"] = payload.params.name;
        }}
      }} else {{
        headers["Accept"] = "application/json, text/event-stream";
        headers["MCP-Protocol-Version"] = "2025-03-26";
      }}
      Object.assign(headers, extraHeaders || {{}});
      const response = await fetch(buildLocalMcpPath(config), {{
        method: "POST",
        headers,
        body: JSON.stringify(payload)
      }});
      const text = await response.text();
      let body = text;
      try {{
        body = JSON.parse(text);
      }} catch (error) {{}}
      return {{
        status: response.status,
        contentType: response.headers.get("content-type"),
        body
      }};
    }}

    function meta2026() {{
      const meta = {{}};
      meta[metaProtocolVersionKey] = protocolVersion2026;
      meta[metaClientCapabilitiesKey] = {{}};
      meta[metaClientInfoKey] = {{ name: "scenario-ui", version: "1.0" }};
      return meta;
    }}

    async function refreshPreview(config) {{
      const title = activeTitle.textContent;
      const group = activeGroup.textContent;
      const era = (config && config.protocolEra === "2026") ? "2026" : "2025";
      preview.textContent = `$ 구분: ${{group}}\\n$ 설정: ${{title}}\\n$ POST /mcp (${{era}})\\n...`;
      try {{
        if (era === "2026") {{
          // No handshake: send tools/list directly with the _meta envelope.
          const toolsList = await postMcp({{
            jsonrpc: "2.0",
            id: 1,
            method: "tools/list",
            params: {{ _meta: meta2026() }}
          }}, config);
          const toolsCall = await postMcp({{
            jsonrpc: "2.0",
            id: 2,
            method: "tools/call",
            params: {{
              name: "convert_length",
              arguments: {{ value: 1, from_unit: "m", to_unit: "cm" }},
              _meta: meta2026()
            }}
          }}, config);
          preview.textContent = [
            `$ 구분: ${{group}}`,
            `$ 설정: ${{title}}`,
            `$ 프로토콜: ${{protocolVersion2026}} (stateless)`,
            `$ 적용 JSON`,
            pretty(config),
            "",
            formatHttp("POST /mcp tools/list", toolsList),
            "",
            formatHttp("POST /mcp tools/call convert_length", toolsCall)
          ].join("\\n");
          return;
        }}
        const initialize = await postMcp({{
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: {{
            protocolVersion: "2025-03-26",
            capabilities: {{}},
            clientInfo: {{ name: "scenario-ui", version: "1.0" }}
          }}
        }}, config);
        const toolsList = await postMcp({{
          jsonrpc: "2.0",
          id: 2,
          method: "tools/list",
          params: {{}}
        }}, config);
        preview.textContent = [
          `$ 구분: ${{group}}`,
          `$ 설정: ${{title}}`,
          `$ 적용 JSON`,
          pretty(config),
          "",
          formatHttp("POST /mcp initialize", initialize),
          "",
          formatHttp("POST /mcp tools/list", toolsList)
        ].join("\\n");
      }} catch (error) {{
        preview.textContent = `$ 설정: ${{title}}\\n${{error && error.stack ? error.stack : error}}`;
      }}
    }}

    function setEra(era, options) {{
      const opts = options || {{}};
      currentEra = era === "2026" ? "2026" : "2025";
      document.body.setAttribute("data-era", currentEra);
      eraButtons.forEach((button) => {{
        button.classList.toggle("is-active", button.dataset.era === currentEra);
      }});
      if (eraHint) {{
        eraHint.textContent = eraHints[currentEra] || "";
      }}
      if (currentEra === "2026") {{
        // 2025-only scenarios are hidden in 2026; clear them so a stale check
        // doesn't leak into collectConfig via the :checked query.
        document.querySelectorAll(".era-2025-only input[type=checkbox]").forEach((input) => {{
          input.checked = false;
        }});
        const cacheInput = document.getElementById("toolsListCacheEnabled");
        if (cacheInput && !opts.silent) {{
          cacheInput.checked = true;
          syncToolsListCacheHint();
        }}
      }} else {{
        // Symmetrically, clear 2026-only toggles when returning to 2025.
        document.querySelectorAll(".era-2026-only input[type=checkbox]").forEach((input) => {{
          input.checked = false;
        }});
        syncRejectLegacyHint();
        syncToolsListCacheHint();
      }}
      if (opts.silent) return;
      // Switching era invalidates 2025-only selections; recollect and re-apply.
      const config = collectConfig();
      appliedConfig = clone(config);
      setControls(appliedConfig);
      updateSummary(appliedConfig, true);
      setGeneratedUrl(appliedConfig, true);
      refreshPreview(appliedConfig);
    }}

    eraButtons.forEach((button) => {{
      button.addEventListener("click", () => setEra(button.dataset.era));
    }});

    applyConfig.addEventListener("click", () => {{
      const config = collectConfig();
      appliedConfig = clone(config);
      setControls(appliedConfig);
      updateSummary(appliedConfig, true);
      setGeneratedUrl(appliedConfig, true);
      flashButton(applyConfig, "URL 생성됨", "적용");
      refreshPreview(appliedConfig);
    }});

    resetConfig.addEventListener("click", () => {{
      const config = clone(defaultConfig);
      appliedConfig = clone(config);
      setControls(appliedConfig);
      updateSummary(appliedConfig, true);
      setGeneratedUrl(appliedConfig, true);
      flashButton(resetConfig, "초기화됨", "초기화");
      refreshPreview(appliedConfig);
    }});

    function handleControlChange(event) {{
      if (event && event.target && event.target.name === "serverErrors") {{
        enforceExclusiveScenario(event.target);
      }}
      const config = collectConfig();
      syncEnabledStates(config);
      updateSummary(config, false);
      markUrlNeedsApply();
    }}

    document.querySelectorAll("input, select").forEach((input) => {{
      input.addEventListener("change", handleControlChange);
      input.addEventListener("input", handleControlChange);
    }});

    document.getElementById("expireSessions").addEventListener("click", async () => {{
      const resultEl = document.getElementById("expireSessionsResult");
      try {{
        const response = await fetch("/scenario", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ action: "expireSessions" }})
        }});
        const body = await response.json();
        resultEl.textContent = body.ok ? `만료 ${{body.expired}}건.` : `실패: ${{body.error}}`;
      }} catch (error) {{
        resultEl.textContent = `실패: ${{error}}`;
      }}
    }});

    copyMcpUrl.addEventListener("click", async () => {{
      const text = mcpUrl.textContent.trim();
      try {{
        await navigator.clipboard.writeText(text);
        copyMcpUrl.textContent = "복사됨";
        setTimeout(() => {{
          copyMcpUrl.textContent = "복사";
        }}, 1200);
      }} catch (error) {{
        copyMcpUrl.textContent = "실패";
        setTimeout(() => {{
          copyMcpUrl.textContent = "복사";
        }}, 1200);
      }}
    }});

    setEra(currentEra, {{ silent: true }});
    setControls(appliedConfig);
    updateSummary(appliedConfig, true);
    setGeneratedUrl(appliedConfig, false);
    refreshPreview(appliedConfig);
  </script>
</body>
</html>"""


# Methods each fixed route accepts, for the Allow header of a 405. /mcp depends on
# the config (Handler.mcp_allowed_methods).
ROUTE_METHODS = {
    "/": "GET, OPTIONS",
    "/scenario": "GET, POST, OPTIONS",
    "/healthz": "GET, OPTIONS",
    "/inspect": "GET, OPTIONS",
    "/inspect/log": "GET, OPTIONS",
    "/.well-known/oauth-protected-resource": "GET, OPTIONS",
    "/.well-known/oauth-authorization-server": "GET, OPTIONS",
    "/authorize": "GET, POST, OPTIONS",
    "/register": "POST, OPTIONS",
    "/token": "POST, OPTIONS",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UnitExpertMCP/1.0"

    def do_OPTIONS(self) -> None:
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
        self.send_405(self.mcp_allowed_methods(config))
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

        if path == "/inspect":
            self.send_html(200, render_inspect_page())
            return

        if path == "/inspect/log":
            self.send_json(200, {"exchanges": inspect_snapshot()})
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
            self.send_405()
            return

        try:
            config = self.request_config()
        except ValueError as error:
            self.send_text(400, str(error))
            return
        if self.handle_pre_json_rpc_config(config):
            return
        if self.enforce_auth(config, None, None):
            return

        accept = self.headers.get("Accept", "")
        if "text/event-stream" not in accept:
            self.send_text(400, "Invalid Accept header. Expected TEXT_EVENT_STREAM")
            return

        session_id = self.headers.get("mcp-session-id")
        if config["session"]["validate"] and config["session"]["issueId"]:
            failure = session_error(session_id)
            if failure is not None:
                record_exchange(
                    "GET",
                    path,
                    self.headers,
                    None,
                    failure[0],
                    failure[1],
                    server_era=config_server_era(config),
                    injected=["session.validate"],
                )
                self.send_json(failure[0], failure[1])
                return
        if not session_id:
            self.send_text(400, "Session ID required in mcp-session-id header")
            return

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
            self.send_405()
            return
        if self.handle_pre_json_rpc_config(config):
            return
        if self.enforce_auth(config, None, None):
            return
        session_id = self.headers.get("mcp-session-id")
        failure = session_error(session_id)
        status = failure[0] if failure is not None else 200
        if failure is None and session_id:
            terminate_session(session_id)
        record_exchange(
            "DELETE",
            path,
            self.headers,
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
            record_exchange("POST", path, self.headers, raw_body.decode("utf-8", "replace"), 400, error)
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
                record_exchange(
                    "POST",
                    path,
                    self.headers,
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
            record_exchange("POST", path, self.headers, payload, 400, error)
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
                    record_exchange(
                        "POST",
                        path,
                        self.headers,
                        payload,
                        400,
                        {"error": "Invalid Accept header. Expected TEXT_EVENT_STREAM"},
                    )
                    self.send_text(400, "Invalid Accept header. Expected TEXT_EVENT_STREAM")
                    return
            elif "application/json" not in accept and "*/*" not in accept:
                record_exchange("POST", path, self.headers, payload, 400,
                                {"error": "Invalid Accept header. Expected APPLICATION_JSON"})
                self.send_text(400, "Invalid Accept header. Expected APPLICATION_JSON")
                return
        elif "application/json" not in accept or "text/event-stream" not in accept:
            self.send_text(400, "Invalid Accept headers. Expected TEXT_EVENT_STREAM and APPLICATION_JSON")
            return

        try:
            config = self.request_config()
        except ValueError as error:
            self.send_text(400, str(error))
            return
        if self.handle_pre_json_rpc_config(config, payload.get("method")):
            return
        auth_status = self.enforce_auth(config, payload.get("method"), payload)
        if auth_status:
            record_exchange(
                "POST",
                path,
                self.headers,
                payload,
                auth_status,
                {"error": "invalid_token" if auth_status == 401 else "insufficient_scope"},
            )
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
                record_exchange("POST", path, self.headers, payload, failure[0], failure[1])
                self.send_json(failure[0], failure[1])
                return
            messages = [
                subscriptions_acknowledged_notification(payload),
                subscriptions_listen_result(payload.get("id"), config),
            ]
            record_exchange("POST", path, self.headers, payload, 200, {"events": messages})
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
        record_exchange(
            "POST",
            path,
            self.headers,
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
        record_exchange(
            http_method, path, self.headers, payload, 403, error, injected=["allowedOrigins"]
        )
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
                    # Actions change runtime state, not the active config.
                    if payload["action"] != "expireSessions":
                        raise ValueError(f"unknown action '{payload['action']}'")
                    self.send_json(
                        200, {"ok": True, "action": "expireSessions", "expired": expire_sessions()}
                    )
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
    ) -> bool:
        server = config["server"]
        if server["delayEnabled"]:
            time.sleep(server["delaySeconds"])

        custom_header = config.get("customHeader", {})
        if custom_header.get("enabled"):
            header_val = self.headers.get(custom_header.get("name", ""))
            if header_val is None:
                self.send_text(401, "Unauthorized")
                return True
            if header_val != custom_header.get("value", ""):
                self.send_text(403, "Forbidden")
                return True
            return False

        http_status = server["httpStatus"]
        target = server["target"]
        if http_status == 200:
            return False
        if target == "initialize" and method not in {None, "initialize"}:
            return False
        if http_status == 401:
            self.send_text(401, "Unauthorized")
            return True
        if http_status == 403:
            self.send_text(403, "Forbidden")
            return True
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
