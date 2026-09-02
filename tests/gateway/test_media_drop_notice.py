"""The user must learn when an attachment was NOT delivered (issue #75065).

A MEDIA path the gateway cannot deliver (typically: the agent produced the
file inside a sandboxed terminal backend, so the path exists in the
container and not on the gateway host) used to be dropped behind a host-side
WARNING. The text went out, the message looked delivered, and both the user
and the agent believed the file had arrived.

These tests pin the notice on the two chat delivery paths: the non-streaming
``BasePlatformAdapter._process_message_background`` and the post-stream
``GatewayRunner._deliver_media_from_response`` rescan (where the text is
already on the wire, so the notice has to be its own message).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _CapturingAdapter(BasePlatformAdapter):
    """Minimal adapter that records what reached the chat."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="fake-token"), Platform.TELEGRAM)
        self.sent: list[dict] = []
        self.documents: list[str] = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content})
        return SendResult(success=True, message_id="msg-1")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def send_document(self, chat_id, file_path, caption=None, **kwargs) -> SendResult:
        self.documents.append(str(file_path))
        return SendResult(success=True, message_id="doc-1")

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


async def _hold_typing(_chat_id, interval=2.0, metadata=None, stop_event=None):
    if stop_event is not None:
        await stop_event.wait()
    else:
        await asyncio.Event().wait()


def _event() -> MessageEvent:
    return MessageEvent(
        text="fa-mi raportul",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="111", chat_type="dm"),
        message_id="m1",
    )


async def _deliver(adapter, response: str) -> None:
    async def handler(_event):
        return response

    adapter.set_message_handler(handler)
    event = _event()
    await adapter._process_message_background(event, build_session_key(event.source))


@pytest.mark.asyncio
async def test_undeliverable_media_appends_notice_to_the_reply(tmp_path):
    adapter = _CapturingAdapter()
    adapter._keep_typing = _hold_typing
    # Shape of the real incident: the agent built the file inside its sandbox,
    # so the path is absolute and plausible but absent on the gateway host.
    ghost = tmp_path / "sandbox-home" / "raport_iulie_2026.xlsx"

    await _deliver(adapter, f"Raportul e gata.\nMEDIA:{ghost}")

    assert len(adapter.sent) == 1, adapter.sent
    content = adapter.sent[0]["content"]
    assert "Raportul e gata." in content
    assert "raport_iulie_2026.xlsx" in content
    assert "Couldn't deliver" in content
    # The directory half is host filesystem layout — it must not be echoed.
    assert "sandbox-home" not in content
    assert str(tmp_path) not in content


@pytest.mark.asyncio
async def test_attachment_only_response_still_reports_the_failure(tmp_path):
    """No text + no deliverable file used to mean: nothing at all is sent."""
    adapter = _CapturingAdapter()
    adapter._keep_typing = _hold_typing
    ghost = tmp_path / "raport.xlsx"

    await _deliver(adapter, f"MEDIA:{ghost}")

    assert len(adapter.sent) == 1, adapter.sent
    assert "Couldn't deliver" in adapter.sent[0]["content"]
    assert "raport.xlsx" in adapter.sent[0]["content"]


@pytest.mark.asyncio
async def test_successful_delivery_adds_no_notice(tmp_path):
    adapter = _CapturingAdapter()
    adapter._keep_typing = _hold_typing
    real = tmp_path / "raport.pdf"
    real.write_bytes(b"%PDF-1.4")

    await _deliver(adapter, f"Uite raportul.\nMEDIA:{real}")

    assert adapter.documents == [str(real.resolve())], adapter.documents
    assert all("Couldn't deliver" not in s["content"] for s in adapter.sent), adapter.sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "![chart](https://example.com/chart.png)",
        "MEDIA:{local_image}",
    ],
)
async def test_failed_non_streaming_image_batch_gets_user_visible_notice(
    response, tmp_path,
):
    adapter = _CapturingAdapter()
    adapter._keep_typing = _hold_typing
    adapter.send_multiple_images = AsyncMock(
        return_value=SendResult(success=False, error="Telegram upload rejected")
    )
    local_image = tmp_path / "chart.png"
    local_image.write_bytes(b"png")

    await _deliver(adapter, response.format(local_image=local_image))

    adapter.send_multiple_images.assert_awaited_once()
    assert len(adapter.sent) == 1, adapter.sent
    assert "Couldn't deliver the image attachment" in adapter.sent[0]["content"]


@pytest.mark.asyncio
async def test_missing_non_streaming_image_batch_result_gets_notice():
    adapter = _CapturingAdapter()
    adapter._keep_typing = _hold_typing
    adapter.send_multiple_images = AsyncMock(return_value=None)

    await _deliver(adapter, "![chart](https://example.com/chart.png)")

    adapter.send_multiple_images.assert_awaited_once()
    assert len(adapter.sent) == 1, adapter.sent
    assert "Couldn't deliver the image attachment" in adapter.sent[0]["content"]


def _fake_runner():
    return SimpleNamespace(
        _thread_metadata_for_source=lambda source, anchor=None: {},
        _reply_anchor_for_event=lambda event: None,
    )


def _stream_adapter():
    return SimpleNamespace(
        name="test",
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        extract_local_files=BasePlatformAdapter.extract_local_files,
        send=AsyncMock(return_value=SendResult(success=True, message_id="text")),
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="doc")),
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="voice")),
        send_image_file=AsyncMock(return_value=SendResult(success=True, message_id="img")),
        send_video=AsyncMock(return_value=SendResult(success=True, message_id="vid")),
        send_multiple_images=AsyncMock(return_value=SendResult(success=True, message_id="imgs")),
    )


@pytest.mark.asyncio
async def test_post_stream_drop_is_sent_as_its_own_message(tmp_path):
    """The reply text is already on the wire, so the notice needs its own send."""
    adapter = _stream_adapter()
    ghost = tmp_path / "sandbox-home" / "raport_iulie_2026.xlsx"

    await GatewayRunner._deliver_media_from_response(
        _fake_runner(), f"MEDIA:{ghost}", _event(), adapter, thread_metadata={},
    )

    adapter.send.assert_awaited_once()
    content = adapter.send.await_args.kwargs["content"]
    assert "raport_iulie_2026.xlsx" in content
    assert "Couldn't deliver" in content
    assert "sandbox-home" not in content
    adapter.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_stream_success_sends_no_notice(tmp_path):
    adapter = _stream_adapter()
    real = tmp_path / "raport.pdf"
    real.write_bytes(b"%PDF-1.4")

    await GatewayRunner._deliver_media_from_response(
        _fake_runner(), f"MEDIA:{real}", _event(), adapter, thread_metadata={},
    )

    adapter.send_document.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_post_stream_image_batch_result_gets_notice(tmp_path):
    adapter = _stream_adapter()
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    adapter.send_multiple_images = AsyncMock(return_value=None)

    await GatewayRunner._deliver_media_from_response(
        _fake_runner(), f"MEDIA:{image}", _event(), adapter, thread_metadata={}
    )

    adapter.send_multiple_images.assert_awaited_once()
    adapter.send.assert_awaited_once()
    assert "Couldn't deliver" in adapter.send.await_args.kwargs["content"]
