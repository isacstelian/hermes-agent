"""Loader-level precedence tests for Telegram bot-origin policy."""

from gateway.config import Platform, load_gateway_config


def _load_config(monkeypatch, tmp_path, yaml_text: str, **env):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(yaml_text, encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TELEGRAM_ALLOW_BOTS", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    return load_gateway_config()


def _resolved_policy(config) -> str:
    from plugins.platforms.telegram.adapter import TelegramAdapter

    telegram_config = config.platforms[Platform.TELEGRAM]
    return TelegramAdapter(telegram_config)._bot_loop_guard_policy()


def test_loader_preserves_canonical_allow_bots(monkeypatch, tmp_path) -> None:
    config = _load_config(
        monkeypatch,
        tmp_path,
        "gateway:\n"
        "  platforms:\n"
        "    telegram:\n"
        "      extra:\n"
        "        allow_bots: mentions\n",
    )

    assert config.platforms[Platform.TELEGRAM].extra["allow_bots"] == "mentions"
    assert _resolved_policy(config) == "mentions"


def test_loader_prefers_canonical_allow_bots_over_environment(
    monkeypatch, tmp_path
) -> None:
    config = _load_config(
        monkeypatch,
        tmp_path,
        "gateway:\n"
        "  platforms:\n"
        "    telegram:\n"
        "      extra:\n"
        "        allow_bots: mentions\n",
        TELEGRAM_ALLOW_BOTS="all",
    )

    assert config.platforms[Platform.TELEGRAM].extra["allow_bots"] == "mentions"
    assert _resolved_policy(config) == "mentions"


def test_loader_preserves_compatibility_allow_bots(monkeypatch, tmp_path) -> None:
    config = _load_config(
        monkeypatch,
        tmp_path,
        "telegram:\n  allow_bots: all\n",
    )

    assert config.platforms[Platform.TELEGRAM].extra["allow_bots"] == "all"
    assert _resolved_policy(config) == "all"


def test_loader_prefers_canonical_allow_bots_on_conflict(monkeypatch, tmp_path) -> None:
    config = _load_config(
        monkeypatch,
        tmp_path,
        "telegram:\n"
        "  allow_bots: all\n"
        "gateway:\n"
        "  platforms:\n"
        "    telegram:\n"
        "      extra:\n"
        "        allow_bots: mentions\n",
    )

    assert config.platforms[Platform.TELEGRAM].extra["allow_bots"] == "mentions"
    assert _resolved_policy(config) == "mentions"


def test_loader_prefers_canonical_none_for_rollback(monkeypatch, tmp_path) -> None:
    config = _load_config(
        monkeypatch,
        tmp_path,
        "telegram:\n"
        "  allow_bots: all\n"
        "gateway:\n"
        "  platforms:\n"
        "    telegram:\n"
        "      extra:\n"
        "        allow_bots: none\n",
    )

    assert config.platforms[Platform.TELEGRAM].extra["allow_bots"] == "none"
    assert _resolved_policy(config) == "none"


def test_loader_keeps_environment_only_allow_bots_fallback(
    monkeypatch, tmp_path
) -> None:
    config = _load_config(
        monkeypatch,
        tmp_path,
        "",
        TELEGRAM_ALLOW_BOTS="all",
        TELEGRAM_BOT_TOKEN="123456789:test-token-value",
    )

    assert config.platforms[Platform.TELEGRAM].extra.get("allow_bots") is None
    assert _resolved_policy(config) == "all"
