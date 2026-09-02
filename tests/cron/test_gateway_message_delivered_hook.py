from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

from cron.scheduler import _deliver_result, _emit_gateway_message_delivered
from gateway.config import GatewayConfig, Platform, PlatformConfig


def _job():
    return {
        "id": "daily-brief",
        "execution_id": "exec-123",
        "definition_hash": "sha256:abc",
        "deliver": "origin",
        "origin": {
            "platform": "telegram",
            "chat_id": "-1001",
            "thread_id": "42",
        },
    }


def test_helper_has_zero_payload_work_without_subscriber():
    job = MagicMock()
    with (
        patch("hermes_cli.lifecycle.has_hook", return_value=False),
        patch("hermes_cli.lifecycle.invoke_hook") as invoke,
    ):
        _emit_gateway_message_delivered(
            job,
            platform="telegram",
            chat_id="-1001",
            thread_id="42",
            message_id="9001",
        )

    invoke.assert_not_called()
    job.get.assert_not_called()


def test_standalone_telegram_delivery_emits_confirmed_message_contract():
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="token")}
    )
    sender = AsyncMock(return_value={"success": True, "message_id": "9001"})

    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch("tools.send_message_tool._send_to_platform", new=sender),
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("hermes_cli.lifecycle.invoke_hook") as invoke,
    ):
        error = _deliver_result(_job(), "Daily brief")

    assert error is None
    invoke.assert_called_once_with(
        "gateway_message_delivered",
        source="cron",
        execution_id="exec-123",
        job_id="daily-brief",
        platform="telegram",
        chat_id="-1001",
        thread_id="42",
        message_id="9001",
    )


def test_live_telegram_delivery_reuses_adapter_message_id():
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="token")}
    )
    adapter = AsyncMock()
    adapter.send.return_value = MagicMock(
        success=True, message_id="live-9002", raw_response=None
    )
    loop = MagicMock()
    loop.is_running.return_value = True

    def run_coro(coro, _loop):
        import asyncio

        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch("asyncio.run_coroutine_threadsafe", side_effect=run_coro),
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("hermes_cli.lifecycle.invoke_hook") as invoke,
    ):
        error = _deliver_result(
            _job(), "Daily brief", adapters={Platform.TELEGRAM: adapter}, loop=loop
        )

    assert error is None
    assert invoke.call_args.kwargs["message_id"] == "live-9002"


def test_failed_standalone_delivery_does_not_emit():
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="token")}
    )
    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch(
            "tools.send_message_tool._send_to_platform",
            new=AsyncMock(return_value={"error": "Telegram unavailable"}),
        ),
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("hermes_cli.lifecycle.invoke_hook") as invoke,
    ):
        error = _deliver_result(_job(), "Daily brief")

    assert "Telegram unavailable" in error
    invoke.assert_not_called()


def test_non_telegram_delivery_does_not_emit():
    job = _job()
    with (
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("hermes_cli.lifecycle.invoke_hook") as invoke,
    ):
        _emit_gateway_message_delivered(
            job,
            platform="discord",
            chat_id="123",
            thread_id=None,
            message_id="456",
        )

    invoke.assert_not_called()


def test_delivery_without_message_id_does_not_emit():
    with (
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("hermes_cli.lifecycle.invoke_hook") as invoke,
    ):
        _emit_gateway_message_delivered(
            _job(),
            platform="telegram",
            chat_id="-1001",
            thread_id=None,
            message_id=None,
        )

    invoke.assert_not_called()


def test_hook_failure_does_not_affect_delivery():
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="token")}
    )
    sender = AsyncMock(return_value={"success": True, "message_id": "9001"})
    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch("tools.send_message_tool._send_to_platform", new=sender),
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("hermes_cli.lifecycle.invoke_hook", side_effect=RuntimeError("plugin broke")),
    ):
        error = _deliver_result(_job(), "Daily brief")

    assert error is None
