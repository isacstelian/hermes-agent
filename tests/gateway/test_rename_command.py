"""Security and behavior contracts for Telegram's /rename command."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _event(
    text: str = "/rename Client follow-up",
    *,
    platform: Platform = Platform.TELEGRAM,
    chat_id: str | None = "12345",
    thread_id: str | None = "67890",
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=platform,
            user_id="employee-1",
            chat_id=chat_id,
            thread_id=thread_id,
            chat_type="group" if thread_id else "dm",
        ),
        message_id="message-1",
    )


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True, token="***", extra={"rename_enabled": True}
            )
        }
    )
    adapter = MagicMock()
    adapter.config = runner.config.platforms[Platform.TELEGRAM]
    adapter.rename_dm_topic = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    return runner, adapter


def test_rename_is_a_gateway_command_not_a_title_alias():
    from hermes_cli.commands import resolve_command

    command = resolve_command("rename")

    assert command is not None
    assert command.name == "rename"
    assert command.cli_only is True
    assert command.gateway_only is True
    assert command.gateway_config_gate == "platforms.telegram.extra.rename_enabled"
    assert command.args_hint == "<name>"


def test_rename_is_hidden_from_gateway_surfaces_unless_profile_flag_is_true(
    monkeypatch,
):
    import hermes_cli.config
    from hermes_cli.commands import gateway_help_lines, telegram_bot_commands

    monkeypatch.setattr(hermes_cli.config, "read_raw_config", lambda: {})
    assert not any(line.startswith("`/rename ") for line in gateway_help_lines())
    assert "rename" not in {name for name, _desc in telegram_bot_commands()}

    monkeypatch.setattr(
        hermes_cli.config,
        "read_raw_config",
        lambda: {
            "platforms": {
                "telegram": {"extra": {"rename_enabled": True}}
            }
        },
    )
    assert any(line.startswith("`/rename ") for line in gateway_help_lines())
    assert "rename" in {name for name, _desc in telegram_bot_commands()}


@pytest.mark.asyncio
async def test_rename_is_disabled_by_default_for_a_profile():
    runner, adapter = _runner()
    adapter.config.extra = {}

    result = await runner._handle_rename_command(_event())

    assert result == "Telegram topic rename is not enabled for this profile."
    adapter.rename_dm_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_rename_uses_only_the_current_event_scope_and_preserves_user_words():
    runner, adapter = _runner()

    result = await runner._handle_rename_command(
        _event("/rename   Client\nfollow-up ✅  ")
    )

    adapter.rename_dm_topic.assert_awaited_once_with(
        chat_id="12345",
        thread_id="67890",
        name="Client follow-up ✅",
    )
    assert result == "Renamed this Telegram topic to “Client follow-up ✅”."


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/rename", "/rename   \n\t"])
async def test_rename_requires_a_non_empty_name(text):
    runner, adapter = _runner()

    result = await runner._handle_rename_command(_event(text))

    adapter.rename_dm_topic.assert_not_awaited()
    assert result == "Usage: /rename <name>"


@pytest.mark.asyncio
async def test_rename_rejects_names_over_telegram_limit_without_truncating():
    runner, adapter = _runner()

    result = await runner._handle_rename_command(_event("/rename " + "x" * 129))

    adapter.rename_dm_topic.assert_not_awaited()
    assert "128 characters" in result
    assert "not renamed" in result


@pytest.mark.asyncio
async def test_rename_rejects_non_telegram_sources():
    runner, adapter = _runner()

    result = await runner._handle_rename_command(
        _event(platform=Platform.DISCORD)
    )

    adapter.rename_dm_topic.assert_not_awaited()
    assert result == "/rename is available only in Telegram."


@pytest.mark.asyncio
async def test_rename_rejects_sources_without_a_topic():
    runner, adapter = _runner()

    result = await runner._handle_rename_command(_event(thread_id=None))

    adapter.rename_dm_topic.assert_not_awaited()
    assert result == "This conversation has no Telegram topic to rename."


@pytest.mark.asyncio
async def test_rename_fails_closed_while_telegram_bot_is_disconnected():
    runner, adapter = _runner()
    adapter._bot = None

    result = await runner._handle_rename_command(_event())

    assert result == "Telegram topic rename is unavailable while the bot is disconnected."
    adapter.rename_dm_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_rename_refuses_operator_managed_dm_topic():
    class ManagedAdapter:
        _bot = object()

        def __init__(self):
            self.config = PlatformConfig(extra={"rename_enabled": True})
            self.rename_dm_topic = AsyncMock()

        def is_operator_managed_dm_topic(self, _chat_id, _thread_id):
            return True

    runner, _adapter = _runner()
    managed = ManagedAdapter()
    runner.adapters = {Platform.TELEGRAM: managed}  # type: ignore[dict-item]

    result = await runner._handle_rename_command(_event())

    assert result == (
        "This Telegram topic has an operator-managed name and cannot be renamed."
    )
    managed.rename_dm_topic.assert_not_awaited()


def test_operator_managed_detection_ignores_runtime_discovered_topic_cache():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter._dm_topics_config = [
        {
            "chat_id": "12345",
            "topics": [{"thread_id": 111, "name": "Configured"}],
        }
    ]

    assert TelegramAdapter.is_operator_managed_dm_topic(adapter, "12345", "111")
    assert not TelegramAdapter.is_operator_managed_dm_topic(adapter, "12345", "67890")


@pytest.mark.asyncio
async def test_rename_fails_closed_without_logging_telegram_secrets(caplog):
    runner, adapter = _runner()
    adapter.rename_dm_topic.side_effect = RuntimeError(
        "https://api.telegram.org/bot12345:SECRET/editForumTopic"
    )

    result = await runner._handle_rename_command(_event())

    assert result == "Telegram did not rename the topic. Its name is unchanged."
    assert "SECRET" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_rename_fails_closed_when_telegram_adapter_is_unavailable():
    runner, _adapter = _runner()
    runner.adapters = {}

    result = await runner._handle_rename_command(_event())

    assert result == "Telegram topic rename is unavailable for this profile."
