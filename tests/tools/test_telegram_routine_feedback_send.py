"""Standalone Telegram delivery preserves routine feedback controls."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig


def _install_telegram_mock(monkeypatch, bot_factory):
    parse_mode = SimpleNamespace(MARKDOWN_V2="MarkdownV2", HTML="HTML")
    constants = SimpleNamespace(ParseMode=parse_mode)
    telegram = SimpleNamespace(
        Bot=bot_factory,
        MessageEntity=lambda **kwargs: SimpleNamespace(**kwargs),
        constants=constants,
    )
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setitem(sys.modules, "telegram.constants", constants)


def _disable_proxy(monkeypatch):
    for name in (
        "TELEGRAM_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
    monkeypatch.setattr(
        "gateway.platforms.base._detect_macos_system_proxy",
        lambda: None,
    )


def test_send_to_platform_forwards_feedback_token_to_telegram(monkeypatch):
    from tools import send_message_tool

    send = AsyncMock(return_value={"success": True, "message_id": "1"})
    monkeypatch.setattr(send_message_tool, "_send_telegram", send)

    result = asyncio.run(
        send_message_tool._send_to_platform(
            Platform.TELEGRAM,
            PlatformConfig(enabled=True, token="token", extra={}),
            "123",
            "Raport",
            args={"routine_feedback_token": "delivery-token"},
        )
    )

    assert result["success"] is True
    assert send.await_args.kwargs["routine_feedback_token"] == "delivery-token"


def test_standalone_text_send_attaches_keyboard_and_returns_actual_thread(
    monkeypatch,
):
    from plugins.platforms.telegram import adapter as telegram_adapter
    from tools.send_message_tool import _send_telegram

    _disable_proxy(monkeypatch)
    markup = object()
    monkeypatch.setattr(
        telegram_adapter,
        "build_routine_feedback_keyboard",
        lambda _token: markup,
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
    _install_telegram_mock(monkeypatch, MagicMock(return_value=bot))

    result = asyncio.run(
        _send_telegram(
            "token",
            "123",
            "Raport",
            thread_id="9",
            routine_feedback_token="delivery-token",
        )
    )

    assert bot.send_message.await_args.kwargs["reply_markup"] is markup
    assert result["message_id"] == "42"
    assert result["feedback_message_id"] == "42"
    assert result["thread_id"] == "9"


@pytest.mark.parametrize("target_thread_id", ["1", None], ids=["explicit", "implicit"])
def test_general_topic_delivery_and_button_vote_use_the_same_coordinates(
    monkeypatch, tmp_path, target_thread_id
):
    from cron import executions
    from plugins.platforms.telegram import adapter as telegram_adapter
    from tools.send_message_tool import _send_telegram

    _disable_proxy(monkeypatch)
    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    monkeypatch.setattr(
        telegram_adapter,
        "build_routine_feedback_keyboard",
        lambda _token: object(),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(
            message_id=42,
            message_thread_id=None,
            is_topic_message=False,
            chat=SimpleNamespace(type="supergroup", is_forum=True),
        )
    )
    _install_telegram_mock(monkeypatch, MagicMock(return_value=bot))

    execution = executions.create_execution("general-topic", source="builtin")
    delivery = executions.record_execution_delivery(
        execution["id"],
        platform="telegram",
        chat_id="-100123",
        status="pending",
    )
    send_result = asyncio.run(
        _send_telegram(
            "token",
            "-100123",
            "Raport",
            thread_id=target_thread_id,
            routine_feedback_token=delivery["id"],
        )
    )
    executions.record_execution_delivery(
        execution["id"],
        delivery_id=delivery["id"],
        platform="telegram",
        chat_id="-100123",
        thread_id=send_result["thread_id"],
        message_id=send_result["feedback_message_id"],
        status="delivered",
    )

    adapter = telegram_adapter.TelegramAdapter(
        PlatformConfig(enabled=True, token="token", extra={})
    )
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    query = SimpleNamespace(
        data=f"cl:rf:u:{delivery['id']}",
        message=SimpleNamespace(
            chat_id=-100123,
            message_id=42,
            message_thread_id=None,
            is_topic_message=False,
            direct_messages_topic=None,
            chat=SimpleNamespace(type="supergroup", is_forum=True),
        ),
        from_user=SimpleNamespace(id=111, first_name="Isac"),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    asyncio.run(
        adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), SimpleNamespace()
        )
    )

    assert "message_thread_id" not in bot.send_message.await_args.kwargs
    assert send_result["thread_id"] == "1"
    assert executions.list_execution_feedback(delivery["id"])[0]["vote"] == 1


@pytest.mark.parametrize("chat_type", ["private", "group"])
def test_implicit_non_forum_delivery_stays_unthreaded(chat_type):
    from tools.send_message_tool import _telegram_delivery_thread_id

    message = SimpleNamespace(
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(type=chat_type, is_forum=False),
    )

    assert _telegram_delivery_thread_id(
        message,
        requested_thread_id=None,
        sent_thread_id=None,
    ) is None


def test_standalone_media_only_send_attaches_keyboard_to_the_visible_message(
    monkeypatch,
):
    from plugins.platforms.telegram import adapter as telegram_adapter
    from tools.send_message_tool import _send_telegram

    _disable_proxy(monkeypatch)
    markup = object()
    monkeypatch.setattr(
        telegram_adapter,
        "build_routine_feedback_keyboard",
        lambda _token: markup,
    )
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=77))
    _install_telegram_mock(monkeypatch, MagicMock(return_value=bot))

    media = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    media.write(b"image")
    media.close()
    try:
        result = asyncio.run(
            _send_telegram(
                "token",
                "123",
                "",
                media_files=[(media.name, False)],
                routine_feedback_token="delivery-token",
            )
        )
    finally:
        os.unlink(media.name)

    assert bot.send_photo.await_args.kwargs["reply_markup"] is markup
    assert result["message_id"] == "77"
    assert result["feedback_message_id"] == "77"


def test_standalone_last_media_failure_reports_no_feedback_message(monkeypatch):
    from plugins.platforms.telegram import adapter as telegram_adapter
    from tools.send_message_tool import _send_telegram

    _disable_proxy(monkeypatch)
    markup = object()
    monkeypatch.setattr(
        telegram_adapter,
        "build_routine_feedback_keyboard",
        lambda _token: markup,
    )
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=10))
    bot.send_photo = AsyncMock(
        side_effect=[
            SimpleNamespace(message_id=20),
            RuntimeError("last upload failed"),
        ]
    )
    _install_telegram_mock(monkeypatch, MagicMock(return_value=bot))

    first = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    second = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    first.write(b"first")
    second.write(b"second")
    first.close()
    second.close()
    try:
        result = asyncio.run(
            _send_telegram(
                "token",
                "123",
                "Raport",
                media_files=[(first.name, False), (second.name, False)],
                routine_feedback_token="delivery-token",
            )
        )
    finally:
        os.unlink(first.name)
        os.unlink(second.name)

    first_call, second_call = bot.send_photo.await_args_list
    assert "reply_markup" not in first_call.kwargs
    assert second_call.kwargs["reply_markup"] is markup
    assert result["message_id"] == "20"
    assert result["feedback_message_id"] is None
    assert "last upload failed" in result["warnings"][0]
