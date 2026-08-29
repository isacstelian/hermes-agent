"""Current Telegram files are opt-in MCP request metadata."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.session_context import (
    clear_session_vars,
    set_current_attachments,
    set_session_vars,
)
from tools import mcp_tool


def _run(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory

    async def call():
        for server in mcp_tool._servers.values():
            server._rpc_lock = asyncio.Lock()
        return await coro

    return asyncio.run(call())


def _server(session, config):
    return SimpleNamespace(
        session=session,
        _config=config,
        _rpc_lock=None,
        _pending_call_context=None,
    )


def _result(text):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        isError=False,
        structuredContent=None,
    )


def test_current_telegram_attachments_are_forwarded_as_host_paths_when_enabled(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "board-denisa"
    source = profile_home / "cache" / "documents" / "raport.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_result("draft"))
    server = _server(session, {"forward_current_attachments": True})
    tokens = set_session_vars(platform="telegram", message_id="42")
    set_current_attachments(
        ["/root/.hermes/cache/documents/raport.pdf"], ["application/pdf"]
    )

    try:
        with patch.dict(mcp_tool._servers, {"magic": server}), patch(
            "tools.mcp_tool._run_on_mcp_loop", side_effect=_run
        ):
            handler = mcp_tool._make_tool_handler("magic", "email_draft_create", 120)
            handler({"include_current_attachments": True})
        session.call_tool.assert_called_once_with(
            "email_draft_create",
            arguments={"include_current_attachments": True},
            meta={
                "ai.hermes/current-attachments": {
                    "platform": "telegram",
                    "message_id": "42",
                    "files": [
                        {
                            "path": str(source),
                            "content_type": "application/pdf",
                        }
                    ],
                }
            },
        )
    finally:
        clear_session_vars(tokens)


def test_current_attachments_are_not_forwarded_without_server_opt_in():
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_result("ok"))
    server = _server(session, {})
    tokens = set_session_vars(platform="telegram", message_id="42")
    set_current_attachments(["/host/cache/secret.pdf"], ["application/pdf"])

    try:
        with patch.dict(mcp_tool._servers, {"external": server}), patch(
            "tools.mcp_tool._run_on_mcp_loop", side_effect=_run
        ):
            handler = mcp_tool._make_tool_handler("external", "read", 120)
            handler({})
        session.call_tool.assert_called_once_with("read", arguments={})
    finally:
        clear_session_vars(tokens)
