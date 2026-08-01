import json
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config


def _adapter(*, enabled: object = True, trusted=None, extra=None):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    settings = dict(extra or {})
    if enabled is not None:
        settings["auto_allow_groups_from_trusted_adders"] = enabled
    if trusted is not None:
        settings["trusted_group_adders"] = trusted

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="not-written", extra=settings)
    adapter._bot = SimpleNamespace(id=999, token="not-written")
    return adapter


def _membership_update(
    *,
    chat_id=-100123,
    chat_type="supergroup",
    actor_id=111,
    old_status="member",
    new_status="administrator",
    member_user_id=999,
):
    member = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="Private team content"),
        from_user=SimpleNamespace(id=actor_id, username="secret-operator"),
        old_chat_member=SimpleNamespace(
            status=old_status,
            user=SimpleNamespace(id=member_user_id),
        ),
        new_chat_member=SimpleNamespace(
            status=new_status,
            user=SimpleNamespace(id=member_user_id),
        ),
    )
    return SimpleNamespace(my_chat_member=member)


@pytest.mark.asyncio
async def test_trusted_admin_promotion_persists_and_extends_effective_allowlists(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(trusted=[111], extra={
        "allowed_chats": ["-200"],
        "group_allowed_chats": ["-300"],
    })

    await adapter._handle_my_chat_member(_membership_update(), None)

    assert adapter._telegram_allowed_chats() == {"-200", "-100123"}
    assert adapter._telegram_group_allowed_chats() == {"-300", "-100123"}

    state_path = tmp_path / "state" / "telegram-auto-authorized-groups.json"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "chat_ids": ["-100123"]
    }
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    raw = state_path.read_text(encoding="utf-8")
    assert "not-written" not in raw
    assert "Private team content" not in raw
    assert "secret-operator" not in raw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update,adapter",
    [
        (_membership_update(actor_id=222), _adapter(trusted=[111])),
        (_membership_update(new_status="member"), _adapter(trusted=[111])),
        (_membership_update(chat_type="channel"), _adapter(trusted=[111])),
        (_membership_update(chat_type="private", chat_id=123), _adapter(trusted=[111])),
        (_membership_update(member_user_id=12345), _adapter(trusted=[111])),
        (_membership_update(), _adapter(enabled=False, trusted=[111])),
        (_membership_update(), _adapter(trusted="111")),
        (_membership_update(), _adapter(trusted=[True, "not-an-id"])),
        (_membership_update(), _adapter(enabled="sometimes", trusted=[111])),
    ],
)
async def test_untrusted_or_malformed_updates_fail_closed(
    monkeypatch, tmp_path, update, adapter
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    await adapter._handle_my_chat_member(update, None)

    assert adapter._telegram_auto_authorized_groups() == set()
    assert not (tmp_path / "state" / "telegram-auto-authorized-groups.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("removed_status", ["member", "restricted", "left", "kicked"])
async def test_transition_away_from_administrator_revokes_regardless_of_actor(
    monkeypatch, tmp_path, removed_status
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(trusted=[111])
    await adapter._handle_my_chat_member(_membership_update(), None)

    await adapter._handle_my_chat_member(
        _membership_update(
            actor_id=222,
            old_status="administrator",
            new_status=removed_status,
        ),
        None,
    )

    assert adapter._telegram_auto_authorized_groups() == set()
    assert adapter._telegram_allowed_chats() == set()
    assert adapter._telegram_group_allowed_chats() == set()


def test_gateway_authz_sees_persisted_group_admission(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "telegram-auto-authorized-groups.json").write_text(
        json.dumps({"chat_ids": ["-100123"]}),
        encoding="utf-8",
    )
    adapter = _adapter(trusted=[111], extra={"group_allowed_chats": ["-200"]})
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-100123",
        chat_type="group",
        user_id=None,
        user_name=None,
    )

    assert runner._is_user_authorized(source) is True
    assert adapter.config.extra["group_allowed_chats"] == ["-200"]


@pytest.mark.asyncio
async def test_malformed_state_fails_closed_without_being_overwritten(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    state_path = state_dir / "telegram-auto-authorized-groups.json"
    state_path.write_text('{"chat_ids": "-100123"}', encoding="utf-8")
    adapter = _adapter(trusted=[111])

    await adapter._handle_my_chat_member(_membership_update(), None)

    assert adapter._telegram_auto_authorized_groups() == set()
    assert state_path.read_text(encoding="utf-8") == '{"chat_ids": "-100123"}'


def test_config_and_env_conventions_are_strict_and_profile_safe(monkeypatch, tmp_path):
    hermes_home = tmp_path / "profile-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "telegram:\n"
        "  auto_allow_groups_from_trusted_adders: true\n"
        "  trusted_group_adders:\n"
        "    - 111\n"
        "    - \"222\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TELEGRAM_AUTO_ALLOW_GROUPS_FROM_TRUSTED_ADDERS", raising=False)
    monkeypatch.delenv("TELEGRAM_TRUSTED_GROUP_ADDERS", raising=False)

    config = load_gateway_config()
    telegram_config = config.platforms[Platform.TELEGRAM]
    adapter = _adapter(extra=telegram_config.extra)

    assert telegram_config.extra["auto_allow_groups_from_trusted_adders"] is True
    assert telegram_config.extra["trusted_group_adders"] == [111, "222"]
    assert adapter._telegram_auto_allow_groups_from_trusted_adders() is True
    assert adapter._telegram_trusted_group_adders() == {111, 222}

    env_adapter = _adapter(enabled=None, extra={})
    monkeypatch.setenv("TELEGRAM_AUTO_ALLOW_GROUPS_FROM_TRUSTED_ADDERS", "true")
    monkeypatch.setenv("TELEGRAM_TRUSTED_GROUP_ADDERS", "333, 444")
    assert env_adapter._telegram_auto_allow_groups_from_trusted_adders() is True
    assert env_adapter._telegram_trusted_group_adders() == {333, 444}


def test_my_chat_member_handler_is_registered_on_each_application(monkeypatch):
    import plugins.platforms.telegram.adapter as telegram_module

    callbacks = []

    class FakeChatMemberHandler:
        MY_CHAT_MEMBER = "my-chat-member"

        def __init__(self, callback, chat_member_types=None):
            callbacks.append((callback, chat_member_types))

    monkeypatch.setattr(telegram_module, "ChatMemberHandler", FakeChatMemberHandler, raising=False)
    monkeypatch.setattr(telegram_module, "TelegramMessageHandler", Mock())
    monkeypatch.setattr(telegram_module, "CallbackQueryHandler", Mock())
    monkeypatch.setattr(
        telegram_module,
        "filters",
        SimpleNamespace(
            TEXT=MagicMock(),
            COMMAND=MagicMock(),
            LOCATION=MagicMock(),
            VENUE=MagicMock(),
            PHOTO=MagicMock(),
            VIDEO=MagicMock(),
            AUDIO=MagicMock(),
            VOICE=MagicMock(),
            Document=SimpleNamespace(ALL=MagicMock()),
            Sticker=SimpleNamespace(ALL=MagicMock()),
        ),
    )
    adapter = _adapter(trusted=[111])
    first_app = SimpleNamespace(add_handler=Mock())
    rebuilt_app = SimpleNamespace(add_handler=Mock())

    adapter._register_handlers(first_app)
    adapter._register_handlers(rebuilt_app)

    assert callbacks == [
        (adapter._handle_my_chat_member, FakeChatMemberHandler.MY_CHAT_MEMBER),
        (adapter._handle_my_chat_member, FakeChatMemberHandler.MY_CHAT_MEMBER),
    ]
    assert first_app.add_handler.call_count == 6
    assert rebuilt_app.add_handler.call_count == 6
