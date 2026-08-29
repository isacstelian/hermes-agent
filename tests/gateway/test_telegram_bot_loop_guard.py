"""Fail-closed Telegram bot-origin loop guard tests (IT-157)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.session import SessionSource
from gateway.telegram_bot_guard import (
    InMemoryTelegramBotGuardStore,
    TelegramBotGuard,
    TelegramBotGuardConfigError,
)


PROFILE = "profile-a"
CHAT_ID = "-100123"
RECEIVER_ID = "200"
RECEIVER_USERNAME = "receiver_bot"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BrokenStore:
    def load(self):
        raise OSError("state backend unavailable")


class MalformedStore:
    def load(self):
        return {"seen": []}


def _raw_message(
    *,
    message_id: str,
    text: str,
    sender_id: str,
    sender_is_bot: bool,
    reply_to_id: str | None = None,
    reply_to_author_id: str | None = None,
    reply_to_author_is_bot: bool | None = None,
):
    reply = None
    if reply_to_id is not None:
        reply_user = None
        if reply_to_author_id is not None:
            reply_user = SimpleNamespace(
                id=int(reply_to_author_id),
                is_bot=bool(reply_to_author_is_bot),
            )
        reply = SimpleNamespace(
            message_id=int(reply_to_id),
            from_user=reply_user,
        )
    return SimpleNamespace(
        message_id=int(message_id),
        text=text,
        caption=None,
        from_user=SimpleNamespace(
            id=int(sender_id),
            is_bot=sender_is_bot,
        ),
        reply_to_message=reply,
    )


def _event(
    *,
    message_id: str = "10",
    text: str = "/status@receiver_bot",
    sender_id: str = "100",
    sender_is_bot: bool = True,
    reply_to_id: str | None = None,
    reply_to_author_id: str | None = None,
    reply_to_author_is_bot: bool | None = None,
) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=CHAT_ID,
        chat_type="group",
        user_id=sender_id,
        user_name="sensitive-person-name",
        is_bot=sender_is_bot,
        profile=PROFILE,
        message_id=message_id,
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id=message_id,
        raw_message=_raw_message(
            message_id=message_id,
            text=text,
            sender_id=sender_id,
            sender_is_bot=sender_is_bot,
            reply_to_id=reply_to_id,
            reply_to_author_id=reply_to_author_id,
            reply_to_author_is_bot=reply_to_author_is_bot,
        ),
    )


def _guard(
    policy: str,
    *,
    clock: Clock | None = None,
    store=None,
) -> TelegramBotGuard:
    return TelegramBotGuard(
        policy=policy,
        clock=clock or Clock(),
        store=store or InMemoryTelegramBotGuardStore(),
    )


def _evaluate(guard: TelegramBotGuard, event: MessageEvent):
    return guard.evaluate(
        event,
        receiver_bot_id=RECEIVER_ID,
        receiver_username=RECEIVER_USERNAME,
    )


def test_none_rejects_bot_origin() -> None:
    decision = _evaluate(_guard("none"), _event())

    assert decision.allowed is False
    assert decision.reason == "policy_none"


def test_mentions_accepts_explicit_command_mention() -> None:
    decision = _evaluate(
        _guard("mentions"),
        _event(text="/status@receiver_bot please"),
    )

    assert decision.allowed is True
    assert decision.reason == "accept"
    assert decision.depth == 1


def test_mentions_rejects_ordinary_or_missing_mention() -> None:
    guard = _guard("mentions")

    ordinary = _evaluate(guard, _event(message_id="11", text="hello @receiver_bot"))
    missing = _evaluate(guard, _event(message_id="12", text="hello"))

    assert ordinary.reason == "mention_drop"
    assert missing.reason == "mention_drop"


def test_mentions_accepts_direct_reply_to_root_bot_message() -> None:
    guard = _guard("mentions")
    guard.note_outbound(
        chat_id=CHAT_ID,
        message_ids=["9"],
        reply_to_message_id=None,
        content="root response",
    )

    decision = _evaluate(
        guard,
        _event(
            message_id="10",
            text="direct reply",
            reply_to_id="9",
            reply_to_author_id=RECEIVER_ID,
            reply_to_author_is_bot=True,
        ),
    )

    assert decision.allowed is True
    assert decision.depth == 1


def test_all_accepts_unmentioned_bot_message() -> None:
    decision = _evaluate(_guard("all"), _event(text="unmentioned"))

    assert decision.allowed is True
    assert decision.depth == 1


def test_dedup_key_expires_after_ten_minutes() -> None:
    clock = Clock()
    guard = _guard("all", clock=clock)
    event = _event(text="unmentioned")

    assert _evaluate(guard, event).allowed is True
    assert _evaluate(guard, event).reason == "duplicate_drop"

    clock.advance(600.01)

    assert _evaluate(guard, event).allowed is True


def test_immediate_second_message_from_pair_is_rate_limited() -> None:
    guard = _guard("all")

    assert _evaluate(guard, _event(message_id="10", text="first")).allowed is True
    decision = _evaluate(guard, _event(message_id="11", text="second"))

    assert decision.allowed is False
    assert decision.reason == "pair_rate_drop"


def test_pair_window_expires_after_sixty_seconds() -> None:
    clock = Clock()
    guard = _guard("all", clock=clock)

    assert _evaluate(guard, _event(message_id="10", text="first")).allowed is True
    clock.advance(60.01)

    assert _evaluate(guard, _event(message_id="11", text="second")).allowed is True


def test_chat_accepts_six_then_drops_seventh() -> None:
    guard = _guard("all")

    for index in range(6):
        decision = _evaluate(
            guard,
            _event(
                message_id=str(20 + index),
                sender_id=str(300 + index),
                text=f"message {index}",
            ),
        )
        assert decision.allowed is True

    seventh = _evaluate(
        guard,
        _event(message_id="30", sender_id="400", text="seventh"),
    )

    assert seventh.allowed is False
    assert seventh.reason == "chat_rate_drop"


def test_direct_reply_to_addressed_bot_message_is_depth_two() -> None:
    guard = _guard("mentions")
    guard.note_outbound(
        chat_id=CHAT_ID,
        message_ids=["9"],
        reply_to_message_id=None,
        content="/work@another_bot",
    )

    decision = _evaluate(
        guard,
        _event(
            message_id="10",
            text="reply",
            reply_to_id="9",
            reply_to_author_id=RECEIVER_ID,
            reply_to_author_is_bot=True,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "depth_drop"
    assert decision.depth == 2


def test_unknown_direct_reply_lineage_fails_closed() -> None:
    decision = _evaluate(
        _guard("mentions"),
        _event(
            text="reply",
            reply_to_id="999",
            reply_to_author_id=RECEIVER_ID,
            reply_to_author_is_bot=True,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "unknown_lineage"


def test_malformed_reply_lineage_fails_closed() -> None:
    decision = _evaluate(
        _guard("all"),
        _event(
            text="reply",
            reply_to_id="999",
            reply_to_author_id=None,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "unknown_lineage"


def test_two_violations_open_chat_breaker_for_ten_minutes() -> None:
    clock = Clock()
    guard = _guard("all", clock=clock)

    assert _evaluate(guard, _event(message_id="10", text="first")).allowed is True
    assert (
        _evaluate(guard, _event(message_id="11", text="second")).reason
        == "pair_rate_drop"
    )
    assert (
        _evaluate(guard, _event(message_id="12", text="third")).reason
        == "pair_rate_drop"
    )

    breaker = _evaluate(
        guard,
        _event(message_id="13", sender_id="101", text="different sender"),
    )
    assert breaker.reason == "breaker_open"

    clock.advance(600.01)

    assert (
        _evaluate(
            guard,
            _event(message_id="14", sender_id="101", text="after breaker"),
        ).allowed
        is True
    )


def test_invalid_policy_is_rejected() -> None:
    with pytest.raises(TelegramBotGuardConfigError):
        _guard("unsafe")


@pytest.mark.parametrize("store", [BrokenStore(), MalformedStore()])
def test_state_store_errors_fail_closed_for_bots(store) -> None:
    decision = _evaluate(_guard("all", store=store), _event(text="state probe"))

    assert decision.allowed is False
    assert decision.reason == "state_error"


def test_state_store_errors_do_not_change_human_routing() -> None:
    decision = _evaluate(
        _guard("all", store=BrokenStore()),
        _event(sender_is_bot=False, sender_id="501", text="human"),
    )

    assert decision.allowed is True
    assert decision.reason == "human"


def test_decision_logs_and_counters_are_sanitized(caplog) -> None:
    caplog.set_level("INFO")
    guard = _guard("mentions")
    sensitive_text = "do-not-log-this-message-body"
    sensitive_id = "777888999"

    decision = _evaluate(
        guard,
        _event(
            text=sensitive_text,
            sender_id=sensitive_id,
        ),
    )

    assert decision.reason == "mention_drop"
    assert guard.counters["mention_drop"] == 1
    assert sensitive_text not in caplog.text
    assert sensitive_id not in caplog.text
    assert "sensitive-person-name" not in caplog.text
    assert "token" not in caplog.text.lower()


def test_malformed_nested_state_fails_closed() -> None:
    store = InMemoryTelegramBotGuardStore()
    store.load().pair_windows[("default", CHAT_ID, "410", RECEIVER_ID)] = "corrupt"  # type: ignore[assignment]
    guard = _guard("all", store=store)

    decision = guard.evaluate(
        _event(message_id="415", sender_id="410"),
        receiver_bot_id=RECEIVER_ID,
        receiver_username=RECEIVER_USERNAME,
    )

    assert decision.allowed is False
    assert decision.reason == "state_error"


def test_malformed_nested_state_key_fails_closed() -> None:
    store = InMemoryTelegramBotGuardStore()
    store.load().seen[("wrong", "shape")] = 2_000.0  # type: ignore[index]
    guard = _guard("all", store=store)

    decision = _evaluate(guard, _event(message_id="416", sender_id="411"))

    assert decision.allowed is False
    assert decision.reason == "state_error"


def test_authz_uses_live_guard_policy_without_process_env(monkeypatch) -> None:
    from gateway.run import GatewayRunner

    monkeypatch.delenv("TELEGRAM_ALLOW_BOTS", raising=False)
    guard = _guard("mentions")
    adapter = SimpleNamespace(_bot_loop_guard=guard)
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_args, **_kwargs: False)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=CHAT_ID,
        chat_type="group",
        user_id="411",
        is_bot=True,
    )

    assert runner._is_user_authorized(source) is True


def test_adapter_builds_guard_from_canonical_extra(monkeypatch) -> None:
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.delenv("TELEGRAM_ALLOW_BOTS", raising=False)
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="not-a-real-token",
            extra={"allow_bots": "mentions"},
        )
    )

    assert adapter._bot_loop_guard is not None
    assert adapter._bot_loop_guard.policy == "mentions"


def test_legacy_yaml_key_seeds_profile_scoped_extra(monkeypatch) -> None:
    from plugins.platforms.telegram.adapter import _apply_yaml_config

    monkeypatch.delenv("TELEGRAM_ALLOW_BOTS", raising=False)

    extras = _apply_yaml_config({}, {"allow_bots": "all"})

    assert extras is not None
    assert extras["allow_bots"] == "all"


def test_successful_adapter_send_result_records_outbound_depth() -> None:
    from plugins.platforms.telegram.adapter import TelegramAdapter

    guard = _guard("mentions")
    adapter = object.__new__(TelegramAdapter)
    adapter._bot_loop_guard = guard

    adapter._record_bot_loop_guard_send(
        SendResult(success=True, message_id="901"),
        chat_id=CHAT_ID,
        reply_to=None,
        content="/ask@peer_bot",
    )

    decision = _evaluate(
        guard,
        _event(
            message_id="902",
            sender_id="320",
            text="reply",
            reply_to_id="901",
            reply_to_author_id=RECEIVER_ID,
            reply_to_author_is_bot=True,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "depth_drop"


def test_outbound_metadata_anchor_records_reply_depth() -> None:
    """Fallback sends carry the reply anchor only in metadata.

    The non-threaded fallback path calls ``adapter.send`` without a
    positional ``reply_to``, so the guard would otherwise classify the
    outbound message as an unaddressed depth-0 root and let the peer
    bot's native reply back in as a fresh one-hop interaction.
    """
    from plugins.platforms.telegram.adapter import TelegramAdapter

    guard = _guard("mentions")
    adapter = object.__new__(TelegramAdapter)
    adapter._bot_loop_guard = guard

    # Seed the inbound bot interaction at depth 1, as if the gateway had
    # already accepted the peer bot's message this turn.
    guard._store.load().message_depths[(CHAT_ID, "900")] = (
        1,
        9_999_999.0,
    )

    adapter._record_bot_loop_guard_send(
        SendResult(success=True, message_id="901"),
        chat_id=CHAT_ID,
        reply_to=None,
        content="plain answer without any bot handle",
        metadata={"reply_to_message_id": "900"},
    )

    decision = _evaluate(
        guard,
        _event(
            message_id="902",
            sender_id="320",
            text="native peer reply",
            reply_to_id="901",
            reply_to_author_id=RECEIVER_ID,
            reply_to_author_is_bot=True,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "depth_drop"


@pytest.mark.asyncio
async def test_send_records_metadata_only_anchor_without_threading_reply() -> None:
    """Fallback final sends preserve guard lineage without visibly threading."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    clock = Clock()
    guard = _guard("mentions", clock=clock)
    inbound = _evaluate(
        guard,
        _event(message_id="900", sender_id="320", text="/status@receiver_bot"),
    )
    assert inbound.allowed is True
    assert inbound.depth == 1
    clock.advance(61)

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="not-a-real-token"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=901)
    )
    adapter._bot_loop_guard = guard
    adapter._rich_messages_enabled = False

    result = await adapter.send(
        CHAT_ID,
        "plain fallback final",
        reply_to=None,
        metadata={"notify": True, "reply_to_message_id": "900"},
    )

    assert result.success is True
    assert adapter._bot.send_message.await_args.kwargs["reply_to_message_id"] is None

    decision = _evaluate(
        guard,
        _event(
            message_id="902",
            sender_id="320",
            text="native peer reply",
            reply_to_id="901",
            reply_to_author_id=RECEIVER_ID,
            reply_to_author_is_bot=True,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "depth_drop"


def _adapter_with_guard(guard: TelegramBotGuard):
    return SimpleNamespace(
        _bot_loop_guard=guard,
        _bot=SimpleNamespace(id=int(RECEIVER_ID), username=RECEIVER_USERNAME),
        _current_bot_username=lambda: RECEIVER_USERNAME,
    )


def _bare_runner(adapter):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    return runner


def test_runner_gate_keeps_human_behavior_when_guard_store_is_broken() -> None:
    runner = _bare_runner(_adapter_with_guard(_guard("all", store=BrokenStore())))

    assert (
        runner._telegram_bot_origin_allowed(
            _event(sender_is_bot=False, sender_id="501", text="human")
        )
        is True
    )


@pytest.mark.asyncio
async def test_native_bot_reply_at_depth_two_stops_before_plugin_dispatch() -> None:
    guard = _guard("mentions")
    guard.note_outbound(
        chat_id=CHAT_ID,
        message_ids=["9"],
        reply_to_message_id=None,
        content="/work@another_bot",
    )
    runner = _bare_runner(_adapter_with_guard(guard))
    event = _event(
        message_id="10",
        text="native reply",
        reply_to_id="9",
        reply_to_author_id=RECEIVER_ID,
        reply_to_author_is_bot=True,
    )

    with patch("hermes_cli.lifecycle.invoke_hook") as invoke_hook:
        result = await runner._handle_message(event)

    assert result is None
    invoke_hook.assert_not_called()


@pytest.mark.asyncio
async def test_startup_restore_replay_dispatches_preaccepted_bot_event_once() -> None:
    guard = _guard("mentions")
    adapter = _adapter_with_guard(guard)
    runner = _bare_runner(adapter)
    runner._profile_adapters = {PROFILE: {Platform.TELEGRAM: adapter}}
    runner._startup_restore_in_progress = True
    runner._startup_restore_queue = []
    runner._scale_to_zero_note_real_inbound = MagicMock()
    event = _event()

    async def replay(replayed: MessageEvent) -> None:
        await runner._handle_message(replayed)

    adapter.handle_message = replay

    with patch(
        "hermes_cli.lifecycle.invoke_hook",
        return_value=[{"action": "skip", "reason": "test"}],
    ) as invoke_hook:
        assert await runner._handle_message(event) is None
        assert runner._startup_restore_queue == [event]
        assert await runner._drain_startup_restore_queue() == 1

    invoke_hook.assert_called_once()
    assert guard.counters["accept"] == 1
    assert guard.counters.get("duplicate_drop", 0) == 0


@pytest.mark.asyncio
async def test_startup_restore_rejects_disallowed_bot_before_queue() -> None:
    guard = _guard("none")
    adapter = _adapter_with_guard(guard)
    runner = _bare_runner(adapter)
    runner._profile_adapters = {PROFILE: {Platform.TELEGRAM: adapter}}
    runner._startup_restore_in_progress = True
    runner._startup_restore_queue = []

    with patch("hermes_cli.lifecycle.invoke_hook") as invoke_hook:
        assert await runner._handle_message(_event()) is None

    assert runner._startup_restore_queue == []
    invoke_hook.assert_not_called()
    assert guard.counters["mention_drop"] == 1


def test_startup_refuses_enabled_policy_without_guard() -> None:
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(
        enabled=True,
        token="not-a-real-token",
        extra={"allow_bots": "mentions"},
    )
    adapter._bot_loop_guard = None
    adapter._bot_loop_guard_config_error = "guard unavailable"
    adapter._set_fatal_error = MagicMock()

    assert adapter._bot_loop_guard_startup_ready() is False
    adapter._set_fatal_error.assert_called_once()


def test_startup_refuses_invalid_policy() -> None:
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(
        enabled=True,
        token="not-a-real-token",
        extra={"allow_bots": "unsafe"},
    )
    adapter._bot_loop_guard = None
    adapter._bot_loop_guard_config_error = "invalid policy"
    adapter._set_fatal_error = MagicMock()

    assert adapter._bot_loop_guard_startup_ready() is False
    adapter._set_fatal_error.assert_called_once()
