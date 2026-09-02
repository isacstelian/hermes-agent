from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cron.scheduler as scheduler
from cron.scheduler import _deliver_result, _emit_gateway_message_delivered
from gateway.config import GatewayConfig, Platform, PlatformConfig


def _job(*, emit_hook=False):
    job = {
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
    if emit_hook:
        job["_gateway_message_delivered_hook"] = True
    return job


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


def test_standalone_telegram_delivery_does_not_emit_gateway_hook():
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
    sender.assert_awaited_once()
    invoke.assert_not_called()


def test_standalone_telegram_text_with_media_does_not_emit_last_media_id(tmp_path):
    media = tmp_path / "report.pdf"
    media.write_bytes(b"%PDF-1.4 test")
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="token")}
    )
    sender = AsyncMock(return_value={"success": True, "message_id": "last-media-id"})

    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch("tools.send_message_tool._send_to_platform", new=sender),
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("hermes_cli.lifecycle.invoke_hook") as invoke,
    ):
        error = _deliver_result(_job(), f"Daily brief\n\nMEDIA:{media}")

    assert error is None
    sender.assert_awaited_once()
    invoke.assert_not_called()


def test_standalone_telegram_media_only_delivery_does_not_emit_text_hook(tmp_path):
    media = tmp_path / "report.pdf"
    media.write_bytes(b"%PDF-1.4 test")
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
        error = _deliver_result(_job(), f"MEDIA:{media}")

    assert error is None
    sender.assert_awaited_once()
    invoke.assert_not_called()


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
            _job(emit_hook=True),
            "Daily brief",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert error is None
    invoke.assert_called_once_with(
        "gateway_message_delivered",
        source="cron",
        execution_id="exec-123",
        job_id="daily-brief",
        platform="telegram",
        chat_id="-1001",
        thread_id="42",
        message_id="live-9002",
    )


def test_live_telegram_media_only_with_default_wrapper_delivers_without_hook(tmp_path):
    media = tmp_path / "report.pdf"
    media.write_bytes(b"%PDF-1.4 test")
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="token")}
    )
    adapter = AsyncMock()
    adapter.send.return_value = MagicMock(
        success=True, message_id="live-wrapper", raw_response=None
    )
    adapter.send_document = AsyncMock(return_value=MagicMock(success=True))
    loop = MagicMock()
    loop.is_running.return_value = True

    def run_coro(coro, _loop):
        import asyncio

        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("cron.scheduler.load_config", return_value={}),
        patch("asyncio.run_coroutine_threadsafe", side_effect=run_coro),
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("hermes_cli.lifecycle.invoke_hook") as invoke,
    ):
        error = _deliver_result(
            _job(),
            f"MEDIA:{media}",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert error is None
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert "Cronjob Response" in sent_text
    invoke.assert_not_called()


def test_live_telegram_mixed_text_media_with_default_wrapper_emits_hook(tmp_path):
    media = tmp_path / "report.pdf"
    media.write_bytes(b"%PDF-1.4 test")
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="token")}
    )
    adapter = AsyncMock()
    adapter.send.return_value = MagicMock(
        success=True, message_id="live-mixed", raw_response=None
    )
    adapter.send_document = AsyncMock(return_value=MagicMock(success=True))
    loop = MagicMock()
    loop.is_running.return_value = True

    def run_coro(coro, _loop):
        import asyncio

        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("cron.scheduler.load_config", return_value={}),
        patch("asyncio.run_coroutine_threadsafe", side_effect=run_coro),
        patch("hermes_cli.lifecycle.has_hook", return_value=True),
        patch("hermes_cli.lifecycle.invoke_hook") as invoke,
    ):
        error = _deliver_result(
            _job(emit_hook=True),
            f"Daily brief\n\nMEDIA:{media}",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert error is None
    adapter.send.assert_awaited_once()
    invoke.assert_called_once()
    assert invoke.call_args.kwargs["message_id"] == "live-mixed"


def test_live_telegram_thread_fallback_reports_actual_thread_none():
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="token")}
    )
    adapter = AsyncMock()
    adapter.send.return_value = MagicMock(
        success=True,
        message_id="live-9003",
        raw_response={"thread_fallback": True, "requested_thread_id": "42"},
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
            _job(emit_hook=True),
            "Daily brief",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert error is not None
    assert "delivered without thread_id" in error
    assert invoke.call_args.kwargs["thread_id"] is None


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
    adapter = AsyncMock()
    adapter.send.return_value = MagicMock(
        success=True, message_id="live-9004", raw_response=None
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
        patch("hermes_cli.lifecycle.invoke_hook", side_effect=RuntimeError("plugin broke")),
    ):
        error = _deliver_result(
            _job(emit_hook=True),
            "Daily brief",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert error is None


def _capture_run_one_job_delivery_flags(monkeypatch, run_job_result):
    delivered = []

    monkeypatch.setattr(
        scheduler, "create_execution", lambda *_a, **_kw: {"id": "exec-flag"}
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda _execution_id: None)
    monkeypatch.setattr(scheduler, "run_job", lambda *_a, **_kw: run_job_result)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_a, **_kw: "/tmp/out.txt")
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda job, content, **_kwargs: delivered.append(
            (content, bool(job.get("_gateway_message_delivered_hook")))
        )
        or None,
    )
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_a, **_kw: True)
    monkeypatch.setattr(scheduler, "finish_execution", lambda *_a, **_kw: None)

    scheduler.run_one_job({"id": "daily-brief", "name": "daily-brief", "deliver": "telegram"})
    return delivered


def test_run_one_job_enables_gateway_hook_for_successful_generated_text(monkeypatch):
    delivered = _capture_run_one_job_delivery_flags(
        monkeypatch, (True, "raw output", "Daily brief", None)
    )

    assert delivered == [("Daily brief", True)]


def test_run_one_job_does_not_mark_media_only_response_for_gateway_hook(
    monkeypatch, tmp_path
):
    media = tmp_path / "report.pdf"
    media.write_bytes(b"%PDF-1.4 test")

    delivered = _capture_run_one_job_delivery_flags(
        monkeypatch, (True, "raw output", f"MEDIA:{media}", None)
    )

    assert delivered == [(f"MEDIA:{media}", False)]


def test_run_one_job_marks_mixed_text_media_response_for_gateway_hook(
    monkeypatch, tmp_path
):
    media = tmp_path / "report.pdf"
    media.write_bytes(b"%PDF-1.4 test")
    final_response = f"Daily brief\n\nMEDIA:{media}"

    delivered = _capture_run_one_job_delivery_flags(
        monkeypatch, (True, "raw output", final_response, None)
    )

    assert delivered == [(final_response, True)]


@pytest.mark.parametrize(
    "error_text",
    [
        "provider failed",
        f"{scheduler.BLOCKED_CONFIG_MARKER} model is not configured",
        f"{scheduler.DRIFT_SKIP_MARKER} global inference config drifted",
    ],
)
def test_run_one_job_disables_gateway_hook_for_failure_config_and_drift_notices(
    monkeypatch, error_text
):
    delivered = _capture_run_one_job_delivery_flags(
        monkeypatch, (False, "raw output", "", error_text)
    )

    assert delivered
    assert delivered[0][1] is False
