"""Behavior tests for Telegram routine feedback buttons."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as telegram_module
from plugins.platforms.telegram.adapter import (
    TelegramAdapter,
    build_routine_feedback_keyboard,
    build_routine_feedback_reason_keyboard,
)


@pytest.fixture(autouse=True)
def _native_keyboard_types(monkeypatch):
    class Button:
        def __init__(self, text, callback_data):
            self.text = text
            self.callback_data = callback_data

    class Markup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    monkeypatch.setattr(telegram_module, "InlineKeyboardButton", Button)
    monkeypatch.setattr(telegram_module, "InlineKeyboardMarkup", Markup)


def _adapter():
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )
    adapter._bot = SimpleNamespace(send_message=AsyncMock())
    adapter._is_callback_user_authorized = Mock(return_value=True)
    return adapter


def _query(
    data,
    *,
    chat_type="private",
    user_id=111,
    thread_id=None,
    is_forum=False,
    direct_messages_topic_id=None,
):
    message = SimpleNamespace(
        chat_id=(
            -100123
            if chat_type in {"group", "supergroup", "channel"}
            else 222
        ),
        message_id=333,
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
        direct_messages_topic=(
            SimpleNamespace(topic_id=direct_messages_topic_id)
            if direct_messages_topic_id is not None
            else None
        ),
        chat=SimpleNamespace(type=chat_type, is_forum=is_forum),
    )
    return SimpleNamespace(
        data=data,
        message=message,
        from_user=SimpleNamespace(id=user_id, first_name="Isac"),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def _callback_data(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def test_feedback_keyboard_uses_rollback_safe_clarify_callbacks():
    token = "a" * 41

    markup = build_routine_feedback_keyboard(token)

    assert [button.text for button in markup.inline_keyboard[0]] == [
        "👍 Util",
        "👎 Nu m-a ajutat",
    ]
    assert _callback_data(markup) == [
        f"cl:rf:u:{token}",
        f"cl:rf:d:{token}",
    ]
    assert all(len(data.encode("utf-8")) <= 64 for data in _callback_data(markup))
    assert all(
        len(data.encode("utf-8")) <= 64
        for data in _callback_data(build_routine_feedback_reason_keyboard(token))
    )


@pytest.mark.parametrize(
    "token",
    ["", "has:colon", "diacritică", "a" * 42],
)
def test_feedback_keyboard_rejects_tokens_that_cannot_be_safely_encoded(token):
    with pytest.raises(ValueError):
        build_routine_feedback_keyboard(token)


@pytest.mark.asyncio
async def test_send_attaches_feedback_only_to_last_visible_chunk():
    adapter = _adapter()
    adapter._bot.send_message.side_effect = [
        SimpleNamespace(message_id=10),
        SimpleNamespace(message_id=11),
    ]
    adapter.truncate_message = Mock(return_value=["first", "last"])

    result = await adapter.send(
        "222",
        "routine output",
        metadata={"notify": True, "routine_feedback_token": "delivery123"},
    )

    first_call, last_call = adapter._bot.send_message.await_args_list
    assert "reply_markup" not in first_call.kwargs
    assert _callback_data(last_call.kwargs["reply_markup"]) == [
        "cl:rf:u:delivery123",
        "cl:rf:d:delivery123",
    ]
    assert result.success is True
    assert result.message_id == "11"


@pytest.mark.asyncio
async def test_rich_routine_keeps_native_formatting_and_feedback_keyboard():
    adapter = _adapter()
    adapter._rich_messages_enabled = True
    adapter._bot.do_api_request = AsyncMock(return_value={"message_id": 12})

    result = await adapter.send(
        "222",
        "| Coloană | Status |\n|---|---|\n| Valoare | Bun |",
        metadata={"notify": True, "routine_feedback_token": "delivery123"},
    )

    adapter._bot.send_message.assert_not_awaited()
    endpoint, = adapter._bot.do_api_request.await_args.args
    payload = adapter._bot.do_api_request.await_args.kwargs["api_kwargs"]
    assert endpoint == "sendRichMessage"
    assert [
        button["callback_data"]
        for button in payload["reply_markup"]["inline_keyboard"][0]
    ] == ["cl:rf:u:delivery123", "cl:rf:d:delivery123"]
    assert result.success is True
    assert result.message_id == "12"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_method", "file_name", "bot_method"),
    [
        ("send_voice", "routine.ogg", "send_voice"),
        ("send_image_file", "routine.png", "send_photo"),
        ("send_document", "routine.pdf", "send_document"),
        ("send_video", "routine.mp4", "send_video"),
    ],
)
async def test_native_media_sends_attach_feedback_keyboard(
    monkeypatch, tmp_path, adapter_method, file_name, bot_method
):
    adapter = _adapter()
    media_path = tmp_path / file_name
    media_path.write_bytes(b"media")
    monkeypatch.setattr(
        telegram_module,
        "_probe_voice_duration_seconds",
        lambda _path: None,
    )
    adapter._bot = SimpleNamespace(
        send_voice=AsyncMock(return_value=SimpleNamespace(message_id=41)),
        send_audio=AsyncMock(return_value=SimpleNamespace(message_id=41)),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=43)),
        send_video=AsyncMock(return_value=SimpleNamespace(message_id=44)),
    )

    result = await getattr(adapter, adapter_method)(
        "222",
        str(media_path),
        metadata={"routine_feedback_token": "delivery123"},
    )

    call = getattr(adapter._bot, bot_method).await_args
    assert _callback_data(call.kwargs["reply_markup"]) == [
        "cl:rf:u:delivery123",
        "cl:rf:d:delivery123",
    ]
    assert result.success is True


@pytest.mark.asyncio
async def test_positive_dm_feedback_is_saved_answered_and_removes_keyboard():
    adapter = _adapter()
    handler = AsyncMock(return_value={"id": "feedback-row"})
    adapter.set_routine_feedback_handler(handler)
    query = _query("cl:rf:u:delivery123")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    handler.assert_awaited_once_with(
        "delivery123",
        vote=1,
        telegram_user_id="111",
        chat_id="222",
        message_id="333",
        thread_id=None,
        reason=None,
    )
    query.answer.assert_awaited_once()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_legacy_rf_feedback_callback_remains_accepted():
    adapter = _adapter()
    handler = AsyncMock(return_value={"id": "feedback-row"})
    adapter.set_routine_feedback_handler(handler)
    query = _query("rf:u:delivery123")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    handler.assert_awaited_once()
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_negative_dm_feedback_opens_structured_reason_buttons():
    adapter = _adapter()
    handler = AsyncMock(return_value={"id": "feedback-row"})
    adapter.set_routine_feedback_handler(handler)
    query = _query("cl:rf:d:delivery123")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    handler.assert_awaited_once_with(
        "delivery123",
        vote=-1,
        telegram_user_id="111",
        chat_id="222",
        message_id="333",
        thread_id=None,
        reason=None,
    )
    reason_markup = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    reason_callbacks = _callback_data(reason_markup)
    assert reason_callbacks == [
        "cl:rf:r:wrong:delivery123",
        "cl:rf:r:irrelevant:delivery123",
        "cl:rf:r:too_long:delivery123",
        "cl:rf:r:repetitive:delivery123",
        "cl:rf:r:unclear_action:delivery123",
        "cl:rf:r:too_frequent:delivery123",
    ]
    assert all(len(data.encode("utf-8")) <= 64 for data in reason_callbacks)
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_negative_reason_is_saved_and_removes_dm_keyboard():
    adapter = _adapter()
    handler = Mock(return_value={"id": "feedback-row"})
    adapter.set_routine_feedback_handler(handler)
    query = _query("cl:rf:r:unclear_action:delivery123")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    handler.assert_called_once_with(
        "delivery123",
        vote=-1,
        telegram_user_id="111",
        chat_id="222",
        message_id="333",
        thread_id=None,
        reason="unclear_action",
    )
    query.answer.assert_awaited_once()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_group_feedback_keeps_shared_keyboard_for_other_users():
    adapter = _adapter()
    handler = AsyncMock(return_value={"id": "feedback-row"})
    adapter.set_routine_feedback_handler(handler)
    query = _query(
        "cl:rf:d:delivery123", chat_type="supergroup", thread_id=9
    )

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    handler.assert_awaited_once_with(
        "delivery123",
        vote=-1,
        telegram_user_id="111",
        chat_id="-100123",
        message_id="333",
        thread_id="9",
        reason=None,
    )
    query.answer.assert_awaited_once()
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_direct_message_feedback_uses_native_topic_id():
    adapter = _adapter()
    adapter.config.extra["auto_allow_groups_from_trusted_adders"] = True
    handler = AsyncMock(return_value={"id": "feedback-row"})
    adapter.set_routine_feedback_handler(handler)
    query = _query(
        "cl:rf:d:delivery123",
        chat_type="channel",
        thread_id=None,
        direct_messages_topic_id=20189,
    )

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    handler.assert_awaited_once_with(
        "delivery123",
        vote=-1,
        telegram_user_id="111",
        chat_id="-100123",
        message_id="333",
        thread_id="20189",
        reason=None,
    )
    reason_markup = query.edit_message_reply_markup.await_args.kwargs[
        "reply_markup"
    ]
    assert _callback_data(reason_markup)[0] == "cl:rf:r:wrong:delivery123"


@pytest.mark.asyncio
async def test_forum_general_feedback_uses_canonical_thread_one():
    adapter = _adapter()
    handler = AsyncMock(return_value={"id": "feedback-row"})
    adapter.set_routine_feedback_handler(handler)
    query = _query(
        "cl:rf:u:delivery123",
        chat_type="supergroup",
        thread_id=None,
        is_forum=True,
    )

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert handler.await_args.kwargs["thread_id"] == "1"


@pytest.mark.asyncio
async def test_unauthorized_feedback_never_reaches_persistence():
    adapter = _adapter()
    adapter._is_callback_user_authorized.return_value = False
    handler = AsyncMock(return_value={"id": "feedback-row"})
    adapter.set_routine_feedback_handler(handler)
    query = _query("cl:rf:u:delivery123")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    handler.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert "autorizat" in query.answer.await_args.kwargs["text"].lower()
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_feedback_is_answered_and_dead_keyboard_is_removed():
    adapter = _adapter()
    handler = AsyncMock(return_value=None)
    adapter.set_routine_feedback_handler(handler)
    query = _query("cl:rf:u:delivery123", chat_type="group")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    query.answer.assert_awaited_once()
    assert "expirat" in query.answer.await_args.kwargs["text"].lower()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "cl:rf:",
        "cl:rf:x:delivery123",
        "cl:rf:u:",
        "cl:rf:r:unknown:delivery123",
    ],
)
async def test_invalid_feedback_callbacks_fail_safely(data):
    adapter = _adapter()
    handler = AsyncMock(return_value={"id": "feedback-row"})
    adapter.set_routine_feedback_handler(handler)
    query = _query(data)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    handler.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert "invalid" in query.answer.await_args.kwargs["text"].lower()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_persistence_failure_is_answered_and_keyboard_stays_retryable():
    adapter = _adapter()
    handler = AsyncMock(side_effect=RuntimeError("database unavailable"))
    adapter.set_routine_feedback_handler(handler)
    query = _query("cl:rf:u:delivery123")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    query.answer.assert_awaited_once()
    assert "salva" in query.answer.await_args.kwargs["text"].lower()
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_handler_persists_in_the_adapters_profile(monkeypatch, tmp_path):
    from cron import executions
    from hermes_constants import get_hermes_home

    adapter = _adapter()
    adapter._hermes_home = tmp_path
    calls = []

    def record(feedback_token, **kwargs):
        calls.append((feedback_token, kwargs, get_hermes_home()))
        return {"id": "feedback-row"}

    monkeypatch.setattr(
        executions, "record_execution_feedback", record, raising=False
    )
    query = _query("cl:rf:u:delivery123")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert calls == [
        (
            "delivery123",
            {
                "vote": 1,
                "telegram_user_id": "111",
                "chat_id": "222",
                "message_id": "333",
                "thread_id": None,
                "reason": None,
            },
            tmp_path,
        )
    ]
