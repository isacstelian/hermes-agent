"""Regression tests for Telegram callback authorization under multiplex routing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.profile_routing import ProfileRoute
from gateway.run import GatewayRunner
from plugins.platforms.telegram.adapter import TelegramAdapter


_AUTH_ENV_KEYS = (
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_CHATS",
    "TELEGRAM_ALLOW_ALL_USERS",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
)


def _clear_auth_env(monkeypatch):
    for key in _AUTH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _multiplex_adapter(monkeypatch, *, served_profiles=("tenant-a", "tenant-b")):
    _clear_auth_env(monkeypatch)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        multiplex_profiles=True,
        profile_routes=[
            ProfileRoute(
                name="tenant-a-chat",
                platform="telegram",
                chat_id="1001",
                profile="tenant-a",
            ),
            ProfileRoute(
                name="unserved-chat",
                platform="telegram",
                chat_id="1999",
                profile="not-served",
            ),
        ],
    )
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_stores = {
        "tenant-a": MagicMock(),
        "tenant-b": MagicMock(),
    }
    runner._profile_adapters = {}

    monkeypatch.setattr(
        "gateway.run._multiplex_profile_homes",
        lambda _config: [(name, f"/profiles/{name}") for name in served_profiles],
    )

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter.gateway_runner = runner
    runner.adapters = {Platform.TELEGRAM: adapter}

    # This is the shape that triggered the production bug: multiplex replaces
    # the bound method with a closure, so handler.__self__ is unavailable.
    adapter._message_handler = lambda _event: None
    assert getattr(adapter._message_handler, "__self__", None) is None
    return adapter, runner


def test_paired_user_is_authorized_from_routed_profile(monkeypatch):
    adapter, runner = _multiplex_adapter(monkeypatch)
    runner.pairing_stores["tenant-a"].is_approved.return_value = True
    runner.pairing_stores["tenant-b"].is_approved.return_value = False

    assert adapter._is_callback_user_authorized(
        "7682945044", chat_id="1001", chat_type="private"
    ) is True
    runner.pairing_stores["tenant-a"].is_approved.assert_called_once_with(
        "telegram", "7682945044"
    )
    runner.pairing_stores["tenant-b"].is_approved.assert_not_called()


def test_pairing_in_different_profile_does_not_authorize_route(monkeypatch):
    adapter, runner = _multiplex_adapter(monkeypatch)
    runner.pairing_stores["tenant-a"].is_approved.return_value = False
    runner.pairing_stores["tenant-b"].is_approved.return_value = True

    assert adapter._is_callback_user_authorized(
        "7682945044", chat_id="1001", chat_type="private"
    ) is False
    runner.pairing_stores["tenant-a"].is_approved.assert_called_once_with(
        "telegram", "7682945044"
    )
    runner.pairing_stores["tenant-b"].is_approved.assert_not_called()


def test_callback_for_unserved_profile_route_is_denied(monkeypatch):
    adapter, runner = _multiplex_adapter(monkeypatch)
    runner.pairing_store.is_approved.return_value = True
    runner.pairing_stores["tenant-a"].is_approved.return_value = True

    assert adapter._is_callback_user_authorized(
        "7682945044", chat_id="1999", chat_type="private"
    ) is False
    runner.pairing_store.is_approved.assert_not_called()
    runner.pairing_stores["tenant-a"].is_approved.assert_not_called()


@pytest.mark.parametrize(
    ("result", "expected"),
    [(True, True), (False, False), ("authorized", False)],
)
def test_registered_callback_requires_literal_boolean(result, expected, monkeypatch):
    _clear_auth_env(monkeypatch)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._message_handler = lambda _event: None
    adapter.set_authorization_check(lambda *_args: result)

    assert adapter._is_callback_user_authorized(
        "123", chat_id="123", chat_type="private"
    ) is expected


def test_registered_callback_exception_denies(monkeypatch):
    _clear_auth_env(monkeypatch)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._message_handler = lambda _event: None

    def broken_check(*_args):
        raise RuntimeError("auth unavailable")

    adapter.set_authorization_check(broken_check)
    assert adapter._is_callback_user_authorized(
        "123", chat_id="123", chat_type="private"
    ) is False


@pytest.mark.asyncio
async def test_denied_slash_confirmation_keeps_pending_state(monkeypatch):
    _clear_auth_env(monkeypatch)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter.set_authorization_check(lambda *_args: False)
    adapter._slash_confirm_state["confirm-1"] = "session-1"

    query = MagicMock()
    query.data = "sc:once:confirm-1"
    query.message.chat_id = 123
    query.message.chat.type = "private"
    query.message.message_thread_id = None
    query.from_user.id = 123
    query.from_user.first_name = "Unauthorized"
    query.answer = AsyncMock()
    update = MagicMock(callback_query=query)

    await adapter._handle_callback_query(update, MagicMock())

    assert adapter._slash_confirm_state["confirm-1"] == "session-1"
    assert "not authorized" in query.answer.await_args.kwargs["text"].lower()
