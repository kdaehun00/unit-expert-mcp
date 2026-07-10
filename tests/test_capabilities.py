from __future__ import annotations

import anyio

from unit_expert_mcp.server import (
    SERVICE_NAME,
    SERVER_NAME,
    mcp,
)


def capabilities() -> object:
    return mcp._mcp_server.create_initialization_options().capabilities


def test_initialization_options_match_playmcp_baseline() -> None:
    options = mcp._mcp_server.create_initialization_options()

    assert options.server_name == SERVER_NAME
    assert capabilities().tools is not None


def test_streamable_http_uses_sessions_by_default() -> None:
    assert mcp.settings.stateless_http is False


def test_tools_include_playmcp_required_metadata() -> None:
    tools = anyio.run(mcp.list_tools)

    assert 3 <= len(tools) <= 10
    assert len({tool.name for tool in tools}) == len(tools)
    for tool in tools:
        assert 1 <= len(tool.name) <= 128
        assert all(character.isalnum() or character in {"_", "-"} for character in tool.name)
        assert "kakao" not in tool.name.lower()
        assert tool.description is not None
        assert SERVICE_NAME in tool.description
        assert len(tool.description) <= 1024
        assert tool.inputSchema is not None
        assert tool.annotations is not None
        assert tool.annotations.title
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
        assert tool.annotations.idempotentHint is True
