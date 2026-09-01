"""Scheduler integration for opt-in Telegram routine feedback."""

from __future__ import annotations

from unittest.mock import Mock

import pytest


def _patch_run_pipeline(
    monkeypatch, scheduler, *, success=True, final="Raport", error=None
):
    delivered = []
    contexts = []

    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda _execution_id: None)
    monkeypatch.setattr(
        scheduler,
        "update_execution_context",
        lambda execution_id, **kwargs: contexts.append((execution_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda _job, *, defer_agent_teardown=None, **_kwargs: (
            success,
            "output",
            final,
            error,
        ),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "/tmp/output.md")
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(scheduler, "finish_execution", lambda *_args, **_kwargs: None)

    def deliver(job, content, **kwargs):
        delivered.append((job, content, kwargs))
        return None

    monkeypatch.setattr(scheduler, "_deliver_result", deliver)
    return contexts, delivered


def test_definition_hash_ignores_run_state_but_tracks_prompt_changes():
    from cron.scheduler import _routine_definition_hash

    job = {
        "id": "daily-brief",
        "name": "Raport zilnic",
        "prompt": "Arată vânzările de ieri",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "repeat": {"times": None, "completed": 7},
        "next_run_at": "2026-09-02T09:00:00+03:00",
        "last_run_at": "2026-09-01T09:00:00+03:00",
        "last_status": "ok",
        "failure_streak": 0,
        "fire_claim": {"at": "now", "by": "worker-a"},
        "execution_id": "execution-a",
    }
    changed_state = {
        **job,
        "next_run_at": "2026-09-03T09:00:00+03:00",
        "last_run_at": "2026-09-02T09:00:00+03:00",
        "last_status": "error",
        "failure_streak": 3,
        "fire_claim": {"at": "later", "by": "worker-b"},
        "execution_id": "execution-b",
        "repeat": {"times": None, "completed": 8},
    }

    assert _routine_definition_hash(changed_state) == _routine_definition_hash(job)
    assert _routine_definition_hash({
        **job,
        "prompt": "Arată vânzările săptămânii",
    }) != (_routine_definition_hash(job))


def test_routine_feedback_is_opt_in_by_default():
    from cron.scheduler import _cron_feedback_enabled

    assert _cron_feedback_enabled() is False


def test_successful_run_passes_execution_token_only_when_feedback_is_enabled(
    monkeypatch,
):
    import cron.scheduler as scheduler

    contexts, delivered = _patch_run_pipeline(monkeypatch, scheduler)
    monkeypatch.setattr(
        scheduler,
        "load_config",
        lambda: {"cron": {"feedback": {"enabled": True}}},
    )

    job = {
        "id": "daily-brief",
        "name": "Raport zilnic",
        "prompt": "Arată vânzările",
        "deliver": "telegram",
        "execution_id": "execution-1",
    }
    assert scheduler.run_one_job(job) is True

    assert contexts == [
        (
            "execution-1",
            {
                "job_name": "Raport zilnic",
                "definition_hash": scheduler._routine_definition_hash(job),
            },
        )
    ]
    assert delivered[0][2]["feedback_execution_id"] == "execution-1"


def test_disabled_feedback_and_failure_alerts_never_receive_controls(monkeypatch):
    import cron.scheduler as scheduler

    _contexts, delivered = _patch_run_pipeline(monkeypatch, scheduler)
    monkeypatch.setattr(scheduler, "load_config", lambda: {})

    assert (
        scheduler.run_one_job({
            "id": "disabled",
            "name": "Fără feedback",
            "deliver": "telegram",
            "execution_id": "execution-disabled",
        })
        is True
    )
    assert delivered[0][2].get("feedback_execution_id") is None

    _contexts, delivered = _patch_run_pipeline(
        monkeypatch,
        scheduler,
        success=False,
        final="",
        error="provider failed",
    )
    monkeypatch.setattr(
        scheduler,
        "load_config",
        lambda: {"cron": {"feedback": {"enabled": True}}},
    )

    assert (
        scheduler.run_one_job({
            "id": "failed",
            "name": "Eroare",
            "deliver": "telegram",
            "execution_id": "execution-failed",
        })
        is True
    )
    assert delivered[0][2].get("feedback_execution_id") is None


def test_silent_success_does_not_create_a_feedback_delivery(monkeypatch):
    import cron.scheduler as scheduler

    _contexts, delivered = _patch_run_pipeline(
        monkeypatch,
        scheduler,
        final="[SILENT]",
    )
    monkeypatch.setattr(
        scheduler,
        "load_config",
        lambda: {"cron": {"feedback": {"enabled": True}}},
    )

    assert (
        scheduler.run_one_job({
            "id": "silent",
            "name": "Nimic nou",
            "deliver": "telegram",
            "execution_id": "execution-silent",
        })
        is True
    )
    assert delivered == []


def test_successful_run_records_delivery_error_separately(monkeypatch):
    import cron.scheduler as scheduler

    _contexts, _delivered = _patch_run_pipeline(monkeypatch, scheduler)
    monkeypatch.setattr(scheduler, "load_config", lambda: {})
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda *_args, **_kwargs: "Telegram send rejected",
    )
    finished = []
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )

    assert scheduler.run_one_job({
        "id": "delivery-failed",
        "deliver": "telegram",
        "execution_id": "execution-delivery-failed",
    }) is True

    assert finished[-1][1]["delivery_outcome"] == "failed"
    assert finished[-1][1]["delivery_error"] == "Telegram send rejected"


def test_interrupted_run_keeps_delivery_outcome_in_execution_ledger(monkeypatch):
    import cron.scheduler as scheduler

    _contexts, _delivered = _patch_run_pipeline(monkeypatch, scheduler)
    monkeypatch.setattr(scheduler, "load_config", lambda: {})
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda *_args, **_kwargs: "Telegram send rejected",
    )
    monkeypatch.setattr(
        scheduler, "_consume_interrupted_flag", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr("cron.jobs.update_job", lambda *_args, **_kwargs: None)
    finished = []
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )

    assert scheduler.run_one_job({
        "id": "shutdown-run",
        "deliver": "telegram",
        "execution_id": "execution-shutdown",
    }) is True

    assert finished[-1][1]["delivery_outcome"] == "failed"
    assert finished[-1][1]["delivery_error"] == "Telegram send rejected"


def test_mixed_case_telegram_target_creates_feedback_receipt(monkeypatch, tmp_path):
    from cron import executions
    import cron.scheduler as scheduler

    monkeypatch.setattr(
        executions,
        "EXECUTIONS_FILE",
        tmp_path / "cron" / "executions.db",
    )
    execution = executions.create_execution("mixed-case", source="test")

    token = scheduler._start_routine_feedback_delivery(
        execution["id"], platform_name="Telegram", chat_id="123"
    )

    assert token is not None
    assert executions.lookup_execution_delivery(token)["status"] == "pending"


def test_live_telegram_delivery_persists_exact_feedback_receipt(monkeypatch, tmp_path):
    import asyncio
    from concurrent.futures import Future
    from types import SimpleNamespace

    from cron import executions
    import cron.scheduler as scheduler
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import SendResult

    monkeypatch.setattr(
        executions,
        "EXECUTIONS_FILE",
        tmp_path / "cron" / "executions.db",
    )
    execution = executions.create_execution("daily-brief", source="test")
    pconfig = PlatformConfig(enabled=True, token="test-token", extra={})
    gateway_config = SimpleNamespace(
        platforms={Platform.TELEGRAM: pconfig},
        get_home_channel=lambda _platform: None,
    )
    adapter = Mock()
    loop = Mock()
    loop.is_running.return_value = True
    captured_metadata = []

    async def deliver(_self, _target, _content, metadata):
        captured_metadata.append(metadata)
        return SendResult(success=True, message_id="telegram-message-42")

    def run_now(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [{"platform": "telegram", "chat_id": "123", "thread_id": "9"}],
    )
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: gateway_config,
    )
    monkeypatch.setattr(
        scheduler, "load_config", lambda: {"cron": {"wrap_response": False}}
    )
    monkeypatch.setattr(
        "gateway.delivery.DeliveryRouter._deliver_to_platform",
        deliver,
    )
    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", run_now)

    assert (
        scheduler._deliver_result(
            {"id": "daily-brief", "deliver": "telegram"},
            "Raportul de azi",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
            feedback_execution_id=execution["id"],
        )
        is None
    )

    token = captured_metadata[0]["routine_feedback_token"]
    receipt = executions.lookup_execution_delivery(token)
    assert receipt["status"] == "delivered"
    assert receipt["execution_id"] == execution["id"]
    assert receipt["chat_id"] == "123"
    assert receipt["thread_id"] == "9"
    assert receipt["message_id"] == "telegram-message-42"


def test_live_media_only_feedback_uses_last_attachment_message(monkeypatch, tmp_path):
    import asyncio
    from concurrent.futures import Future
    from types import SimpleNamespace

    from cron import executions
    import cron.scheduler as scheduler
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import SendResult

    monkeypatch.setattr(
        executions,
        "EXECUTIONS_FILE",
        tmp_path / "cron" / "executions.db",
    )
    media_root = tmp_path / "media"
    media_root.mkdir()
    image_path = media_root / "report.png"
    document_path = media_root / "details.pdf"
    image_path.write_bytes(b"image")
    document_path.write_bytes(b"document")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (media_root,),
    )

    execution = executions.create_execution("media-brief", source="test")
    pconfig = PlatformConfig(enabled=True, token="test-token", extra={})
    gateway_config = SimpleNamespace(
        platforms={Platform.TELEGRAM: pconfig},
        get_home_channel=lambda _platform: None,
    )
    media_calls = []

    class Adapter:
        platform = Platform.TELEGRAM

        async def send_image_file(self, **kwargs):
            media_calls.append(("image", kwargs["metadata"]))
            return SendResult(success=True, message_id="media-41")

        async def send_document(self, **kwargs):
            media_calls.append(("document", kwargs["metadata"]))
            return SendResult(success=True, message_id="media-42")

    loop = Mock()
    loop.is_running.return_value = True

    def run_now(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [{"platform": "telegram", "chat_id": "123"}],
    )
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: gateway_config,
    )
    monkeypatch.setattr(
        scheduler, "load_config", lambda: {"cron": {"wrap_response": False}}
    )
    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", run_now)

    assert (
        scheduler._deliver_result(
            {"id": "media-brief", "deliver": "telegram"},
            f"MEDIA:{image_path}\nMEDIA:{document_path}",
            adapters={Platform.TELEGRAM: Adapter()},
            loop=loop,
            feedback_execution_id=execution["id"],
        )
        is None
    )

    assert "routine_feedback_token" not in (media_calls[0][1] or {})
    token = media_calls[1][1]["routine_feedback_token"]
    receipt = executions.lookup_execution_delivery(token)
    assert receipt["status"] == "delivered"
    assert receipt["message_id"] == "media-42"


@pytest.mark.parametrize("delivery_kind", ["text", "media"])
def test_live_gateway_implicit_general_callback_saves_feedback(
    monkeypatch, tmp_path, delivery_kind
):
    import asyncio
    from concurrent.futures import Future
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cron import executions
    import cron.scheduler as scheduler
    from gateway.config import Platform, PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setattr(
        executions,
        "EXECUTIONS_FILE",
        tmp_path / "cron" / "executions.db",
    )
    media_root = tmp_path / "media"
    media_root.mkdir()
    document_path = media_root / "report.pdf"
    document_path.write_bytes(b"report")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (media_root,),
    )

    execution = executions.create_execution(
        f"implicit-general-{delivery_kind}", source="test"
    )
    pconfig = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={"rich_messages": False},
    )
    gateway_config = SimpleNamespace(
        platforms={Platform.TELEGRAM: pconfig},
        get_home_channel=lambda _platform: None,
    )
    adapter = TelegramAdapter(pconfig)
    adapter._hermes_home = tmp_path
    adapter._is_callback_user_authorized = Mock(return_value=True)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=43)),
    )
    adapter._bot = bot
    loop = Mock()
    loop.is_running.return_value = True

    def run_now(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [
            {"platform": "telegram", "chat_id": "-100123", "thread_id": None}
        ],
    )
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: gateway_config,
    )
    monkeypatch.setattr(
        scheduler, "load_config", lambda: {"cron": {"wrap_response": False}}
    )
    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", run_now)

    content = (
        "Raportul de azi"
        if delivery_kind == "text"
        else f"MEDIA:{document_path}"
    )
    assert scheduler._deliver_result(
        {"id": execution["job_id"], "deliver": "telegram"},
        content,
        adapters={Platform.TELEGRAM: adapter},
        loop=loop,
        feedback_execution_id=execution["id"],
    ) is None

    sent = (
        bot.send_message.await_args.kwargs
        if delivery_kind == "text"
        else bot.send_document.await_args.kwargs
    )
    message_id = str(42 if delivery_kind == "text" else 43)
    callback_data = sent["reply_markup"].inline_keyboard[0][0].callback_data
    token = callback_data.rsplit(":", 1)[-1]
    assert executions.lookup_execution_delivery(token)["thread_id"] is None

    query = SimpleNamespace(
        data=callback_data,
        message=SimpleNamespace(
            chat_id=-100123,
            message_id=int(message_id),
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

    assert executions.list_execution_feedback(token)[0]["vote"] == 1
    assert executions.lookup_execution_delivery(token)["thread_id"] == "1"


def test_live_text_plus_media_failure_does_not_link_feedback_to_plain_text(
    monkeypatch, tmp_path
):
    import asyncio
    from concurrent.futures import Future
    from types import SimpleNamespace

    from cron import executions
    import cron.scheduler as scheduler
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import SendResult

    monkeypatch.setattr(
        executions,
        "EXECUTIONS_FILE",
        tmp_path / "cron" / "executions.db",
    )
    media_root = tmp_path / "media"
    media_root.mkdir()
    document_path = media_root / "details.pdf"
    document_path.write_bytes(b"document")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (media_root,),
    )

    execution = executions.create_execution("mixed-media", source="test")
    pconfig = PlatformConfig(enabled=True, token="test-token", extra={})
    gateway_config = SimpleNamespace(
        platforms={Platform.TELEGRAM: pconfig},
        get_home_channel=lambda _platform: None,
    )
    media_metadata = []

    class Adapter:
        platform = Platform.TELEGRAM

        async def send_document(self, **kwargs):
            media_metadata.append(kwargs["metadata"])
            return SendResult(success=False, error="upload rejected")

    async def deliver(_self, _target, _content, _metadata):
        return SendResult(success=True, message_id="plain-text-41")

    def run_now(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [{"platform": "telegram", "chat_id": "123"}],
    )
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: gateway_config)
    monkeypatch.setattr(
        scheduler, "load_config", lambda: {"cron": {"wrap_response": False}}
    )
    monkeypatch.setattr(
        "gateway.delivery.DeliveryRouter._deliver_to_platform", deliver
    )
    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", run_now)

    error = scheduler._deliver_result(
        {"id": "mixed-media", "deliver": "telegram"},
        f"Raport\nMEDIA:{document_path}",
        adapters={Platform.TELEGRAM: Adapter()},
        loop=Mock(),
        feedback_execution_id=execution["id"],
    )

    token = media_metadata[0]["routine_feedback_token"]
    receipt = executions.lookup_execution_delivery(token)
    assert "upload rejected" in error
    assert receipt["status"] == "failed"
    assert receipt["message_id"] is None


def test_failed_telegram_delivery_persists_the_receipt_error(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from cron import executions
    import cron.scheduler as scheduler
    from gateway.config import Platform, PlatformConfig
    from tools import send_message_tool

    monkeypatch.setattr(
        executions,
        "EXECUTIONS_FILE",
        tmp_path / "cron" / "executions.db",
    )
    execution = executions.create_execution("daily-brief", source="test")
    pconfig = PlatformConfig(enabled=True, token="test-token", extra={})
    gateway_config = SimpleNamespace(
        platforms={Platform.TELEGRAM: pconfig},
        get_home_channel=lambda _platform: None,
    )
    captured = {}

    async def fail_send(*_args, **kwargs):
        captured.update(kwargs)
        return {"error": "send rejected"}

    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [{"platform": "telegram", "chat_id": "123"}],
    )
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: gateway_config,
    )
    monkeypatch.setattr(
        scheduler, "load_config", lambda: {"cron": {"wrap_response": False}}
    )
    monkeypatch.setattr(send_message_tool, "_send_to_platform", fail_send)

    error = scheduler._deliver_result(
        {"id": "daily-brief", "deliver": "telegram"},
        "Raportul de azi",
        feedback_execution_id=execution["id"],
    )

    token = captured["args"]["routine_feedback_token"]
    receipt = executions.lookup_execution_delivery(token)
    assert "send rejected" in error
    assert receipt["status"] == "failed"
    assert "send rejected" in receipt["error"]


def test_standalone_partial_media_failure_does_not_link_plain_message(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from cron import executions
    import cron.scheduler as scheduler
    from gateway.config import Platform, PlatformConfig
    from tools import send_message_tool

    monkeypatch.setattr(
        executions,
        "EXECUTIONS_FILE",
        tmp_path / "cron" / "executions.db",
    )
    execution = executions.create_execution("partial-media", source="test")
    pconfig = PlatformConfig(enabled=True, token="test-token", extra={})
    gateway_config = SimpleNamespace(
        platforms={Platform.TELEGRAM: pconfig},
        get_home_channel=lambda _platform: None,
    )
    captured = {}

    async def partial_send(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "message_id": "plain-message-20",
            "feedback_message_id": None,
            "warnings": ["last media failed"],
        }

    monkeypatch.setattr(
        scheduler,
        "_resolve_delivery_targets",
        lambda _job: [{"platform": "telegram", "chat_id": "123"}],
    )
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: gateway_config)
    monkeypatch.setattr(
        scheduler, "load_config", lambda: {"cron": {"wrap_response": False}}
    )
    monkeypatch.setattr(send_message_tool, "_send_to_platform", partial_send)

    error = scheduler._deliver_result(
        {"id": "partial-media", "deliver": "telegram"},
        "Raport",
        feedback_execution_id=execution["id"],
    )

    token = captured["args"]["routine_feedback_token"]
    receipt = executions.lookup_execution_delivery(token)
    assert "last media failed" in error
    assert receipt["status"] == "failed"
    assert receipt["message_id"] is None
