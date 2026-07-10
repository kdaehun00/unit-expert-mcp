from __future__ import annotations

import mcp.server.session as mcp_server_session
import mcp.server.streamable_http as mcp_streamable_http
import mcp.shared.version as mcp_version
import mcp.types as mcp_types
import pytest

from unit_expert_mcp.server import (
    DEFAULT_PROTOCOL_VERSIONS,
    _parse_protocol_versions,
    _supported_protocol_versions_for,
    configure_supported_protocol_versions,
)


def restore_protocol_versions(supported_versions: list[str], latest_version: str) -> None:
    mcp_version.SUPPORTED_PROTOCOL_VERSIONS[:] = supported_versions
    mcp_types.LATEST_PROTOCOL_VERSION = latest_version


def test_supported_protocol_versions_include_default_versions() -> None:
    assert _supported_protocol_versions_for(DEFAULT_PROTOCOL_VERSIONS) == [
        "2025-03-26",
        "2025-06-18",
        "2025-11-25",
    ]


def test_parse_protocol_versions_from_comma_separated_value() -> None:
    assert _parse_protocol_versions("2025-03-26,2025-11-25") == (
        "2025-03-26",
        "2025-11-25",
    )


def test_configure_supported_protocol_versions_updates_sdk_negotiation_state() -> None:
    original_supported_versions = list(mcp_version.SUPPORTED_PROTOCOL_VERSIONS)
    original_latest_version = mcp_types.LATEST_PROTOCOL_VERSION

    try:
        configured = configure_supported_protocol_versions(("2024-11-05",))

        assert configured == ("2024-11-05",)
        assert mcp_version.SUPPORTED_PROTOCOL_VERSIONS == ["2024-11-05"]
        assert mcp_server_session.SUPPORTED_PROTOCOL_VERSIONS == ["2024-11-05"]
        assert mcp_streamable_http.SUPPORTED_PROTOCOL_VERSIONS == ["2024-11-05"]
        assert mcp_types.LATEST_PROTOCOL_VERSION == "2024-11-05"
    finally:
        restore_protocol_versions(original_supported_versions, original_latest_version)


def test_rejects_unknown_protocol_version() -> None:
    with pytest.raises(ValueError, match="unsupported protocol version"):
        _supported_protocol_versions_for(("2025-01-01",))
