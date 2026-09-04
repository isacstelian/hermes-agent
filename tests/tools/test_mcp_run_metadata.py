"""Run-scoped identity metadata forwarding for MCP tool calls."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tools.mcp_tool as mcp


def _run_on_fresh_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    return asyncio.run(coro)


def _connected_server(session):
    lock = MagicMock()
    lock.__aenter__ = AsyncMock(return_value=None)
    lock.__aexit__ = AsyncMock(return_value=None)
    return SimpleNamespace(
        session=session,
        _rpc_lock=lock,
        _pending_call_context=None,
    )


@pytest.fixture
def mcp_server():
    name = "employee-support-metadata-test"
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text="ok")],
            structuredContent=None,
        )
    )
    previous = mcp._servers.get(name)
    mcp._servers[name] = _connected_server(session)
    try:
        yield name, session
    finally:
        if previous is None:
            mcp._servers.pop(name, None)
        else:
            mcp._servers[name] = previous


def test_run_metadata_reenters_the_shared_mcp_loop_without_leaking():
    metadata = {"employee_binding": "opaque-binding", "location_id": 5}

    async def _read_metadata():
        return mcp.get_run_mcp_metadata()

    token = mcp.set_run_mcp_metadata(metadata)
    try:
        assert asyncio.run(mcp._wrap_with_run_mcp_metadata(_read_metadata())) == metadata
    finally:
        mcp.reset_run_mcp_metadata(token)

    assert mcp.get_run_mcp_metadata() is None


def test_only_explicit_true_enables_metadata_forwarding():
    assert mcp._forward_run_metadata_enabled({"forward_run_metadata": True})
    assert not mcp._forward_run_metadata_enabled({})
    assert not mcp._forward_run_metadata_enabled({"forward_run_metadata": "true"})
    assert not mcp._forward_run_metadata_enabled({"forward_run_metadata": False})


def test_opted_in_server_receives_employee_metadata_in_mcp_meta(mcp_server):
    name, session = mcp_server
    metadata = {"employee_binding": "opaque-binding", "location_id": 5}
    handler = mcp._make_tool_handler(
        name,
        "magic_get_my_income_summary",
        5,
        forward_run_metadata=True,
    )

    token = mcp.set_run_mcp_metadata(metadata)
    try:
        with patch.object(mcp, "_run_on_mcp_loop", side_effect=_run_on_fresh_loop):
            result = handler({"period": "current"})
    finally:
        mcp.reset_run_mcp_metadata(token)

    assert json.loads(result) == {"result": "ok"}
    session.call_tool.assert_awaited_once_with(
        "magic_get_my_income_summary",
        arguments={"period": "current"},
        meta={"magic.employee": metadata},
    )


def test_non_opted_in_server_never_receives_run_metadata(mcp_server):
    name, session = mcp_server
    handler = mcp._make_tool_handler(
        name,
        "magic_get_my_income_summary",
        5,
        forward_run_metadata=False,
    )

    token = mcp.set_run_mcp_metadata({"employee_binding": "opaque-binding"})
    try:
        with patch.object(mcp, "_run_on_mcp_loop", side_effect=_run_on_fresh_loop):
            result = handler({"period": "current"})
    finally:
        mcp.reset_run_mcp_metadata(token)

    assert json.loads(result) == {"result": "ok"}
    session.call_tool.assert_awaited_once_with(
        "magic_get_my_income_summary",
        arguments={"period": "current"},
    )
