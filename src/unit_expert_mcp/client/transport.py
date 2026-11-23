"""Raw HTTP transport for the MCP test client.

Requests are assembled by hand on top of ``http.client`` so that a scenario can
send exactly the bytes it wants, including requests that break the spec on
purpose. Both ``application/json`` and ``text/event-stream`` responses are
parsed (2026-07-28 Streamable HTTP: the server picks either per request and the
client MUST support both).
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

MASK = "***"
# Header names whose values are credentials and must never be printed.
SENSITIVE_HEADER_RE = re.compile(r"authorization|token|secret|cookie|api-?key", re.IGNORECASE)
SENSITIVE_BODY_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "code_verifier",
}

# Form fields (OAuth token/authorize requests) and URL query parameters that carry
# credentials. "code" is the authorization code; it is not masked in JSON bodies,
# where "code" is the JSON-RPC error code.
SENSITIVE_FORM_KEYS = SENSITIVE_BODY_KEYS | {"code", "token"}
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"

HeaderList = list[tuple[str, "str | bytes"]]


def mask_header_value(name: str, value: "str | bytes") -> str:
    """Hide credential values; keep the auth scheme so the output stays readable."""
    text = value.decode("latin-1", "replace") if isinstance(value, bytes) else str(value)
    if name.lower() == "location":
        return mask_url(text)  # an OAuth redirect carries the authorization code
    if not SENSITIVE_HEADER_RE.search(name):
        return text
    if name.lower() == "www-authenticate":
        return text  # a challenge, not a credential
    scheme, _, rest = text.partition(" ")
    if rest and name.lower() in {"authorization", "proxy-authorization"}:
        return f"{scheme} {MASK}"
    return MASK


def mask_json(value: Any) -> Any:
    """Recursively mask token-bearing keys in a JSON value (for -v / --json output)."""
    if isinstance(value, dict):
        return {
            key: (MASK if key in SENSITIVE_BODY_KEYS and child else mask_json(child))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [mask_json(child) for child in value]
    return value


def mask_form(text: str) -> str:
    """Mask credential values in an application/x-www-form-urlencoded body.

    Only the values of SENSITIVE_FORM_KEYS change; everything else is kept as sent.
    """
    parts = []
    for part in text.split("&"):
        key, separator, value = part.partition("=")
        if separator and value and unquote_plus(key) in SENSITIVE_FORM_KEYS:
            part = f"{key}={MASK}"
        parts.append(part)
    return "&".join(parts)


def encode_cfg_token(config: dict[str, Any]) -> str:
    """Encode a server config the way server.py's decode_config_token reads it.

    base64url(compact UTF-8 JSON) without padding, sent as the ``cfg`` query
    parameter (server.py: CONFIG_QUERY_PARAM / decode_config_token).
    """
    body = json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def with_query_param(url: str, name: str, value: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if key != name
    ]
    query.append((name, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class SseEvent:
    event: str | None
    data: str
    id: str | None = None
    json: Any = None


@dataclass
class Exchange:
    """One HTTP request and whatever came back."""

    method: str
    url: str
    request_headers: HeaderList
    request_body: bytes | None
    status: int | None = None
    reason: str = ""
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    events: list[SseEvent] = field(default_factory=list)
    message: Any = None  # final JSON-RPC message (or the plain JSON body)
    notifications: list[Any] = field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str | None = None  # transport-level failure (timeout, refused, ...)
    label: str = ""
    request_id: Any = None  # JSON-RPC id of the request, when the caller knows it
    stream_open: bool = False  # SSE stream still open when the read timeout hit

    # -- response accessors ---------------------------------------------------
    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None

    @property
    def content_type(self) -> str:
        return (self.header("Content-Type") or "").split(";")[0].strip().lower()

    @property
    def is_sse(self) -> bool:
        return self.content_type == "text/event-stream"

    @property
    def rpc_error(self) -> dict[str, Any] | None:
        if isinstance(self.message, dict) and isinstance(self.message.get("error"), dict):
            return self.message["error"]
        return None

    @property
    def rpc_code(self) -> int | None:
        error = self.rpc_error
        code = error.get("code") if error else None
        return code if isinstance(code, int) else None

    @property
    def result(self) -> dict[str, Any] | None:
        if isinstance(self.message, dict) and isinstance(self.message.get("result"), dict):
            return self.message["result"]
        return None

    @property
    def result_type(self) -> str | None:
        """Declared resultType, or None when absent (callers apply the 'complete' default)."""
        result = self.result
        value = result.get("resultType") if result else None
        return value if isinstance(value, str) else None

    @property
    def id_mismatch(self) -> bool:
        """True when the response carries an id other than the request's.

        JSON-RPC: a response MUST carry the id of its request, so such a message
        is not the answer to this request. An error with a null id is allowed
        (the server could not read the request id).
        """
        message = self.message
        if self.request_id is None or not isinstance(message, dict):
            return False
        if "result" not in message and "error" not in message:
            return False
        if message.get("id") is None and "error" in message:
            return False
        return message.get("id") != self.request_id

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    # -- presentation -----------------------------------------------------------
    @property
    def request_is_form(self) -> bool:
        for name, value in self.request_headers:
            if name.lower() == "content-type":
                text = value.decode("latin-1", "replace") if isinstance(value, bytes) else str(value)
                return text.split(";")[0].strip().lower() == FORM_CONTENT_TYPE
        return False

    def masked_request_body(self) -> Any:
        """Request body for -v / --json: parsed JSON or text, credentials masked."""
        if not self.request_body:
            return None
        if self.request_is_form:
            return mask_form(self.request_body.decode("utf-8", "replace"))
        request = self.request_json()
        if request is not None:
            return mask_json(request)
        return self.request_body.decode("utf-8", "replace")

    def request_json(self) -> Any:
        if not self.request_body:
            return None
        try:
            return json.loads(self.request_body)
        except (ValueError, UnicodeDecodeError):
            return None

    def summary(self) -> dict[str, Any]:
        """Compact, credential-free view used by the text and JSON reporters."""
        request = self.request_json()
        info: dict[str, Any] = {
            "label": self.label,
            "request": f"{self.method} {mask_url(self.url)}",
            "rpcMethod": request.get("method") if isinstance(request, dict) else None,
            "status": self.status,
            "contentType": self.content_type or None,
            "rpcCode": self.rpc_code,
            "rpcMessage": (self.rpc_error or {}).get("message"),
            "resultType": self.result_type,
            "elapsedMs": round(self.elapsed_ms, 1),
        }
        if self.error:
            info["error"] = self.error
        interesting = {}
        for name in (
            "Mcp-Session-Id",
            "MCP-Protocol-Version",
            "WWW-Authenticate",
            "Allow",
            "Location",
        ):
            value = self.header(name)
            if value is not None:
                interesting[name] = mask_header_value(name, value)
        if interesting:
            info["responseHeaders"] = interesting
        if self.events:
            info["sseEvents"] = len(self.events)
        if self.stream_open:
            info["streamOpen"] = True
        return info

    def detail(self) -> dict[str, Any]:
        """Full (masked) request/response for -v and --json."""
        response_body: Any
        if self.events:
            response_body = [
                mask_json(event.json) if event.json is not None else event.data
                for event in self.events
            ]
        else:
            try:
                response_body = mask_json(json.loads(self.body)) if self.body else None
            except (ValueError, UnicodeDecodeError):
                response_body = self.text
        return {
            **self.summary(),
            "requestHeaders": [
                [name, mask_header_value(name, value)] for name, value in self.request_headers
            ],
            "requestBody": self.masked_request_body(),
            "allResponseHeaders": [
                [name, mask_header_value(name, value)] for name, value in self.headers
            ],
            "responseBody": response_body,
        }


def mask_url(url: str) -> str:
    """Hide token-like query parameters (access_token, code, ...) in printed URLs."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [
        (key, MASK if key in SENSITIVE_FORM_KEYS and value else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(query, safe="*")))


def parse_sse(raw: str) -> list[SseEvent]:
    """Parse a complete text/event-stream payload into events (WHATWG SSE rules)."""
    events: list[SseEvent] = []
    event_name: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if data_lines:
            events.append(_make_event(event_name, "\n".join(data_lines), event_id))
        event_name, data_lines = None, []

    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            flush()
            continue
        if line.startswith(":"):
            continue  # comment / keep-alive
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            data_lines.append(value)
        elif name == "event":
            event_name = value
        elif name == "id":
            event_id = value
    flush()
    return events


def _make_event(event_name: str | None, data: str, event_id: str | None) -> SseEvent:
    try:
        parsed = json.loads(data)
    except ValueError:
        parsed = None
    return SseEvent(event=event_name, data=data, id=event_id, json=parsed)


def _is_final_response(message: Any, request_id: Any) -> bool:
    if not isinstance(message, dict) or ("result" not in message and "error" not in message):
        return False
    return request_id is None or message.get("id") == request_id or message.get("id") is None


def send(
    method: str,
    url: str,
    headers: HeaderList,
    body: bytes | None = None,
    *,
    timeout: float = 30.0,
    label: str = "",
    request_id: Any = None,
    max_sse_events: int = 200,
) -> Exchange:
    """Send one request exactly as given and collect the response.

    Nothing is added to ``headers`` except Host and Content-Length. For SSE the
    stream is read until the response for ``request_id`` arrives (the final
    response SHOULD end the stream), the server closes it, or the timeout hits.
    """
    exchange = Exchange(
        method=method,
        url=url,
        request_headers=list(headers),
        request_body=body,
        label=label,
        request_id=request_id,
    )
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        exchange.error = f"unsupported URL: {url}"
        return exchange
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    started = time.perf_counter()
    connection: http.client.HTTPConnection | None = None
    try:
        if parts.scheme == "https":
            connection = http.client.HTTPSConnection(
                parts.hostname, port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
        connection.putrequest(method, path, skip_accept_encoding=True)
        for name, value in headers:
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        exchange.status = response.status
        exchange.reason = response.reason
        exchange.headers = list(response.getheaders())
        if exchange.is_sse:
            _read_sse(response, exchange, request_id, max_sse_events)
        else:
            exchange.body = response.read()
            _parse_json_body(exchange)
    except (socket.timeout, TimeoutError):
        if exchange.status is not None and exchange.is_sse and exchange.events:
            # Events arrived and the server keeps the stream open (subscriptions/listen
            # does): what was read is the result, not a transport failure.
            exchange.stream_open = True
        else:
            exchange.error = f"timeout after {timeout:g}s"
    except (OSError, http.client.HTTPException, ValueError) as error:
        exchange.error = f"{type(error).__name__}: {error}"
    finally:
        exchange.elapsed_ms = (time.perf_counter() - started) * 1000
        if connection is not None:
            connection.close()
    return exchange


def _parse_json_body(exchange: Exchange) -> None:
    if not exchange.body:
        return
    try:
        exchange.message = json.loads(exchange.body)
    except (ValueError, UnicodeDecodeError):
        exchange.message = None


def _read_sse(
    response: http.client.HTTPResponse, exchange: Exchange, request_id: Any, limit: int
) -> None:
    chunks: list[bytes] = []
    pending: list[bytes] = []
    try:
        while len(exchange.events) < limit:
            line = response.readline()
            if not line:
                break
            chunks.append(line)
            pending.append(line)
            if line in (b"\n", b"\r\n"):
                done = _absorb_events(exchange, b"".join(pending), request_id)
                pending = []
                if done:
                    break
        if pending:
            _absorb_events(exchange, b"".join(pending) + b"\n", request_id)
    finally:
        exchange.body = b"".join(chunks)


def _absorb_events(exchange: Exchange, raw: bytes, request_id: Any) -> bool:
    """Add parsed events to the exchange; True once the final response is seen."""
    finished = False
    for event in parse_sse(raw.decode("utf-8", "replace")):
        exchange.events.append(event)
        if _is_final_response(event.json, request_id):
            exchange.message = event.json
            finished = True
        elif event.json is not None:
            exchange.notifications.append(event.json)
    return finished
