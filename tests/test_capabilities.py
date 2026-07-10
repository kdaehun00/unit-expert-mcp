from __future__ import annotations

import anyio
import mcp.types as mcp_types

from unit_expert_mcp.server import (
    SERVICE_NAME,
    configure_failing_tools_list_result,
    configure_null_tools_list_result,
    configure_scenario_tools_list_result,
    configure_tools_capability,
    mcp,
)


def capabilities() -> object:
    return mcp._mcp_server.create_initialization_options().capabilities


def test_tools_capability_is_advertised_by_default() -> None:
    configure_tools_capability(True)

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


def test_tools_capability_can_be_hidden_from_initialize_result() -> None:
    original_handler = mcp._mcp_server.request_handlers.get(mcp_types.ListToolsRequest)

    try:
        configure_tools_capability(False)

        assert capabilities().tools is None
    finally:
        if original_handler is not None:
            mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest] = original_handler
        configure_tools_capability(True)


def test_tools_list_can_return_null_tools_for_client_tests() -> None:
    original_handler = mcp._mcp_server.request_handlers.get(mcp_types.ListToolsRequest)

    try:
        configure_null_tools_list_result(True)

        handler = mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest]
        result = anyio.run(handler, mcp_types.ListToolsRequest())

        assert capabilities().tools is not None
        assert result.model_dump(by_alias=True, mode="json", exclude_none=True) == {"tools": None}
    finally:
        if original_handler is not None:
            mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest] = original_handler
        configure_null_tools_list_result(False)


def test_tools_list_can_return_error_for_client_failure_tests() -> None:
    original_handler = mcp._mcp_server.request_handlers.get(mcp_types.ListToolsRequest)

    try:
        configure_failing_tools_list_result(True)

        handler = mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest]
        result = anyio.run(handler, mcp_types.ListToolsRequest())

        assert capabilities().tools is not None
        assert isinstance(result, mcp_types.ErrorData)
        assert result.code == mcp_types.INTERNAL_ERROR
        assert result.message == "Injected tools/list failure"
    finally:
        if original_handler is not None:
            mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest] = original_handler
        configure_failing_tools_list_result(False)


def test_tools_list_can_use_default_scenario_for_headerless_client_tests() -> None:
    original_handler = mcp._mcp_server.request_handlers.get(mcp_types.ListToolsRequest)

    try:
        configure_scenario_tools_list_result("duplicate-tool-name")

        handler = mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest]
        result = anyio.run(handler, mcp_types.ListToolsRequest())
        payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)

        assert [tool["name"] for tool in payload["tools"]] == [
            "search_place",
            "search_place",
        ]
    finally:
        configure_scenario_tools_list_result("ok")
        if original_handler is not None:
            mcp._mcp_server.request_handlers[mcp_types.ListToolsRequest] = original_handler
