from unittest.mock import patch


from cron.scheduler import _emit_gateway_message_delivered


def test_delivery_hook_uses_confirmed_telegram_payload():
    job = {"id": "job-1", "execution_id": "exec-1"}

    with (
        patch(
            "hermes_cli.plugins.has_hook",
            side_effect=lambda name: name == "gateway_message_delivered",
        ),
        patch("hermes_cli.plugins.invoke_hook") as invoke,
    ):
        _emit_gateway_message_delivered(
            job,
            platform="telegram",
            chat_id=123,
            thread_id=42,
            message_id=99,
        )

    invoke.assert_called_once_with(
        "gateway_message_delivered",
        source="cron",
        execution_id="exec-1",
        job_id="job-1",
        platform="telegram",
        chat_id="123",
        thread_id="42",
        message_id="99",
    )


def test_delivery_hook_ignores_unconfirmed_or_non_telegram():
    job = {"id": "job-1", "execution_id": "exec-1"}

    with (
        patch("hermes_cli.plugins.has_hook", return_value=True),
        patch("hermes_cli.plugins.invoke_hook") as invoke,
    ):
        _emit_gateway_message_delivered(
            job,
            platform="discord",
            chat_id=123,
            thread_id=None,
            message_id=99,
        )
        _emit_gateway_message_delivered(
            job,
            platform="telegram",
            chat_id=123,
            thread_id=None,
            message_id=None,
        )

    invoke.assert_not_called()
