"""Authorization-code + PKCE flow against the unit-expert server's own mock
Authorization Server (server.py: /register, /authorize, /token).

The consent step posts ``action=approve&consent=1`` straight to /authorize, which
only the mock AS accepts, so this flow is limited to an AS on the target's origin.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .clients import Target
from .transport import Exchange, origin_of

REDIRECT_URI = "http://localhost/callback"
FORM = [("Content-Type", "application/x-www-form-urlencoded"), ("Accept", "application/json")]


@dataclass
class OAuthFlow:
    steps: dict[str, Exchange] = field(default_factory=dict)
    resource_metadata: dict[str, Any] = field(default_factory=dict)
    server_metadata: dict[str, Any] = field(default_factory=dict)
    client_id: str | None = None
    tokens: dict[str, Any] = field(default_factory=dict)
    refreshed: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def access_token(self) -> str | None:
        token = self.tokens.get("access_token")
        return token if isinstance(token, str) else None


def _json(exchange: Exchange) -> dict[str, Any]:
    return exchange.message if isinstance(exchange.message, dict) else {}


def _get(target: Target, url: str, label: str) -> Exchange:
    return target.send(
        "GET", [("Accept", "application/json")], None, label=label, url=url, bearer=None
    )


def _post_form(target: Target, url: str, form: dict[str, str], label: str) -> Exchange:
    return target.send(
        "POST", list(FORM), urlencode(form).encode(), label=label, url=url, bearer=None
    )


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def run_flow(
    target: Target, *, scope: str = "mcp", resource_metadata_url: str | None = None
) -> OAuthFlow:
    """Discover (RFC 9728 -> RFC 8414), register, authorize, exchange the code."""
    flow = OAuthFlow()
    origin = origin_of(target.url)
    metadata_url = resource_metadata_url or f"{origin}/.well-known/oauth-protected-resource"
    if origin_of(metadata_url) != origin:
        flow.error = (
            f"resource metadata가 다른 origin({origin_of(metadata_url)})에 있어 진행하지 않음"
        )
        return flow

    flow.steps["resource-metadata"] = step = _get(
        target, metadata_url, "oauth: protected resource metadata"
    )
    flow.resource_metadata = _json(step)
    servers = flow.resource_metadata.get("authorization_servers")
    if step.status != 200 or not isinstance(servers, list) or not servers:
        flow.error = "protected resource metadata에 authorization_servers가 없음"
        return flow
    issuer = str(servers[0]).rstrip("/")
    if origin_of(issuer) != origin:
        flow.error = (
            f"인증 서버({issuer})가 대상과 다른 origin이라 진행하지 않음(자체 mock AS 한정)"
        )
        return flow

    flow.steps["server-metadata"] = step = _get(
        target,
        f"{issuer}/.well-known/oauth-authorization-server",
        "oauth: authorization server metadata",
    )
    flow.server_metadata = meta = _json(step)
    endpoints = [
        meta.get(key)
        for key in ("registration_endpoint", "authorization_endpoint", "token_endpoint")
    ]
    if step.status != 200 or not all(
        isinstance(url, str) and origin_of(url) == origin for url in endpoints
    ):
        flow.error = "authorization server metadata에 같은 origin의 register/authorize/token 엔드포인트가 없음"
        return flow
    register_url, authorize_url, token_url = endpoints

    body = json.dumps(
        {"redirect_uris": [REDIRECT_URI], "client_name": "unit-expert-client"}
    ).encode()
    flow.steps["register"] = step = target.send(
        "POST",
        [("Content-Type", "application/json"), ("Accept", "application/json")],
        body,
        label="oauth: register",
        url=register_url,
        bearer=None,
    )
    client_id = _json(step).get("client_id")
    if step.status not in (200, 201) or not isinstance(client_id, str):
        flow.error = "client 등록 실패"
        return flow
    flow.client_id = client_id

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(8)
    flow.steps["authorize"] = step = _post_form(
        target,
        authorize_url,
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "scope": scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "action": "approve",
            "consent": "1",
        },
        "oauth: authorize (approve)",
    )
    query = parse_qs(urlsplit(step.header("Location") or "").query)
    code = (query.get("code") or [None])[0]
    if step.status != 302 or not code or (query.get("state") or [None])[0] != state:
        flow.error = "authorize가 code와 state를 담은 302를 돌려주지 않음"
        return flow

    flow.steps["token"] = step = _post_form(
        target,
        token_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        "oauth: token (authorization_code)",
    )
    flow.tokens = _json(step)
    if step.status != 200 or not flow.access_token:
        flow.error = "token 엔드포인트가 access_token을 주지 않음"
    return flow


def refresh(target: Target, flow: OAuthFlow) -> Exchange | None:
    token_url = flow.server_metadata.get("token_endpoint")
    refresh_token = flow.tokens.get("refresh_token")
    if not isinstance(token_url, str) or not isinstance(refresh_token, str):
        return None
    step = _post_form(
        target,
        token_url,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": flow.client_id or "",
        },
        "oauth: token (refresh_token)",
    )
    flow.steps["refresh"] = step
    flow.refreshed = _json(step)
    return step
