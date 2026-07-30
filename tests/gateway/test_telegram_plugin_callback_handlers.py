"""Tests for plugin-registered Telegram callback handlers."""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


# Minimal python-telegram-bot stubs so the adapter imports without the package.
_fake_error = types.ModuleType("telegram.error")
_fake_error.TelegramError = type("TelegramError", (Exception,), {})
_fake_error.BadRequest = type("BadRequest", (_fake_error.TelegramError,), {})
_fake_error.NetworkError = type("NetworkError", (_fake_error.TelegramError,), {})
_fake_constants = types.ModuleType("telegram.constants")
_fake_constants.ParseMode = SimpleNamespace(HTML="HTML")
_fake_request = types.ModuleType("telegram.request")
_fake_request.HTTPXRequest = type("HTTPXRequest", (), {"__init__": lambda *a, **kw: None})
_fake_ext = types.ModuleType("telegram.ext")
_fake_ext.ApplicationBuilder = type(
    "ApplicationBuilder",
    (),
    {"token": lambda self, *a: self, "build": lambda self: None},
)
_fake_telegram = types.ModuleType("telegram")
_fake_telegram.error = _fake_error
_fake_telegram.constants = _fake_constants
_fake_telegram.ext = _fake_ext
_fake_telegram.request = _fake_request

for _name, _module in [
    ("telegram", _fake_telegram),
    ("telegram.error", _fake_error),
    ("telegram.constants", _fake_constants),
    ("telegram.request", _fake_request),
    ("telegram.ext", _fake_ext),
]:
    sys.modules.setdefault(_name, _module)

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _context(name="test-plugin"):
    manager = PluginManager()
    manifest = PluginManifest(name=name, version="0.1", description="test")
    return manager, PluginContext(manifest=manifest, manager=manager)


def _adapter(*, authorized=True):
    adapter = object.__new__(TelegramAdapter)
    config = PlatformConfig(enabled=True, token="fake-token")
    adapter.config = config
    adapter._config = config
    adapter.platform = Platform.TELEGRAM
    adapter._platform = Platform.TELEGRAM
    adapter._connected = True
    adapter._is_callback_user_authorized = MagicMock(return_value=authorized)
    return adapter


def _query():
    return SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
    )


async def _dispatch(adapter, query, data):
    return await adapter._handle_plugin_callback(
        query,
        data,
        query_chat_id=100,
        query_chat_type="private",
        query_thread_id=None,
        query_user_name="Isac",
    )


def test_register_and_accessor_copy():
    manager, ctx = _context()

    async def callback(query, data):
        return None

    ctx.register_telegram_callback_handler("dz:", callback)
    handlers = manager.get_telegram_callback_handlers()
    assert handlers == [("dz:", callback, "test-plugin")]
    handlers.clear()
    assert len(manager.get_telegram_callback_handlers()) == 1


def test_registration_rejects_bad_and_duplicate_prefixes():
    manager, ctx = _context("one")

    async def callback(*_):
        return None

    ctx.register_telegram_callback_handler("dz:", callback)

    with pytest.raises(ValueError, match="empty prefix"):
        ctx.register_telegram_callback_handler(" ", callback)
    with pytest.raises(ValueError, match="non-callable"):
        ctx.register_telegram_callback_handler("other:", None)
    with pytest.raises(ValueError, match="synchronous"):
        ctx.register_telegram_callback_handler("sync:", lambda *_: None)
    with pytest.raises(ValueError, match="reserved"):
        ctx.register_telegram_callback_handler("gt:", callback)
    with pytest.raises(ValueError, match="reserved"):
        ctx.register_telegram_callback_handler("gt:mail:", callback)

    second = PluginContext(
        manifest=PluginManifest(name="two", version="0.1", description="test"),
        manager=manager,
    )
    with pytest.raises(ValueError, match="already registered"):
        second.register_telegram_callback_handler("dz:", callback)


def test_authorized_callback_receives_query_and_data():
    adapter = _adapter(authorized=True)
    query = _query()
    seen = {}

    async def callback(received_query, data):
        seen["query"] = received_query
        seen["data"] = data

    manager = MagicMock()
    manager.get_telegram_callback_handlers.return_value = [
        ("dz:", callback, "design-loop")
    ]
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        assert asyncio.run(_dispatch(adapter, query, "dz:s:RUN:b:1")) is True

    assert seen == {"query": query, "data": "dz:s:RUN:b:1"}


def test_unauthorized_callback_is_denied_before_plugin():
    adapter = _adapter(authorized=False)
    query = _query()
    callback = AsyncMock()
    manager = MagicMock()
    manager.get_telegram_callback_handlers.return_value = [
        ("dz:", callback, "design-loop")
    ]
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        assert asyncio.run(_dispatch(adapter, query, "dz:s:RUN:b:1")) is True

    callback.assert_not_awaited()
    assert "not authorized" in query.answer.await_args.kwargs["text"]


def test_longest_prefix_wins_and_plugin_failure_is_isolated():
    adapter = _adapter(authorized=True)
    query = _query()
    broad = AsyncMock()

    async def broken(_query, _data):
        raise RuntimeError("boom")

    manager = MagicMock()
    manager.get_telegram_callback_handlers.return_value = [
        ("d:", broad, "broad"),
        ("dz:", broken, "broken"),
    ]
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        assert asyncio.run(_dispatch(adapter, query, "dz:s:RUN:b:1")) is True

    broad.assert_not_awaited()
    assert "failed" in query.answer.await_args.kwargs["text"].lower()


def test_unknown_callback_returns_false():
    adapter = _adapter(authorized=True)
    query = _query()
    manager = MagicMock()
    manager.get_telegram_callback_handlers.return_value = []
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        assert asyncio.run(_dispatch(adapter, query, "unknown")) is False


def test_unknown_core_prefix_reaches_plugin_fallback():
    adapter = _adapter(authorized=True)
    adapter._handle_plugin_callback = AsyncMock(return_value=True)
    query = SimpleNamespace(
        data="dz:s:RUN:b:1",
        from_user=SimpleNamespace(id=42, first_name="Isac"),
        message=SimpleNamespace(
            chat_id=100,
            chat=SimpleNamespace(type="private"),
            message_thread_id=None,
        ),
    )
    update = SimpleNamespace(callback_query=query)

    asyncio.run(adapter._handle_callback_query(update, None))

    adapter._handle_plugin_callback.assert_awaited_once()
    assert adapter._handle_plugin_callback.await_args.args[:2] == (
        query,
        "dz:s:RUN:b:1",
    )
