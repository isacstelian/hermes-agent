"""Telegram outage contracts through real media handlers, caches and SDK messages."""

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
# Gateway conftest installs SDK stubs; this file exercises real SDK messages.
for _name in list(sys.modules):
    if _name == "telegram" or _name.startswith("telegram."):
        del sys.modules[_name]

from telegram import Message
from telegram.error import BadRequest, RetryAfter, TimedOut

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waiters = []

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + seconds, future))
        await future

    async def advance(self, seconds):
        end = self.now + seconds
        for _ in range(2000):
            # Let I/O-free handler tasks reach their next clock boundary.
            for _ in range(10):
                await asyncio.sleep(0)
            # Flush the real executor used by duration probing before moving
            # virtual time; no wall-clock timing assumption under gate load.
            await asyncio.to_thread(lambda: None)
            for _ in range(10):
                await asyncio.sleep(0)
            waiting = [(at, f) for at, f in self.waiters if not f.done()]
            if not waiting or min(at for at, _ in waiting) > end:
                self.now = end
                return
            self.now = min(at for at, _ in waiting)
            for at, future in waiting:
                if at <= self.now:
                    future.set_result(None)
        pytest.fail("Retry loop did not make bounded progress")


def make_bot(download=None):
    bot = SimpleNamespace(id=99, username="test_bot")
    bot.get_file = AsyncMock(return_value=SimpleNamespace(
        file_path="voice.ogg",
        download_as_bytearray=download or AsyncMock(return_value=bytearray(b"ogg-data")),
    ))
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(message_id=100))
    bot.send_audio = AsyncMock(return_value=SimpleNamespace(message_id=101))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=102))
    return bot


def message(bot, kind="voice", caption=None, message_id=42, **extra):
    data = {
        "message_id": message_id, "date": 1794129657,
        "chat": {"id": 100, "type": "private"},
        "from": {"id": 1, "is_bot": False, "first_name": "Fixture"},
        kind: {"file_id": f"file-{message_id}", "file_unique_id": f"unique-{message_id}",
               "file_size": 8, "duration": 1, "file_name": "report.pdf",
               "mime_type": "application/pdf", "width": 1, "height": 1},
        **extra,
    }
    if kind == "photo":
        data[kind] = [data[kind]]
    if caption:
        data["caption"] = caption
    return Message.de_json(data, bot)


@pytest_asyncio.fixture
async def rig(monkeypatch, tmp_path):
    clock = Clock()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = make_bot()
    adapter._running = True
    adapter._is_user_authorized_from_message = lambda _: True
    adapter._should_process_message = lambda _: True
    adapter.handle_message = AsyncMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="notice"))
    adapter._media_retry_now = lambda: clock.now
    adapter._media_retry_sleep = clock.sleep
    monkeypatch.setenv("HERMES_TIMEZONE", "Europe/Bucharest")
    monkeypatch.setattr("hermes_time.get_timezone", lambda: None)
    monkeypatch.setattr("plugins.platforms.telegram.adapter._probe_voice_duration_seconds", lambda _: 1)
    yield adapter, clock
    await adapter.disconnect()


async def receive(adapter, msg):
    await adapter._handle_media_message(SimpleNamespace(message=msg, update_id=7), None)


def lost_lines(caplog):
    return [r.getMessage() for r in caplog.records if "media-retry LOST" in r.getMessage()]


def assert_voice(adapter):
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert len(event.media_urls) == 1
    assert Path(event.media_urls[0]).suffix == ".ogg"
    assert Path(event.media_urls[0]).read_bytes() == b"ogg-data"
    assert event.media_types == ["audio/ogg"]
    return event


@pytest.mark.asyncio
async def test_download_two_timeouts_then_delivers_without_human_notice(rig):
    adapter, clock = rig
    download = adapter._bot.get_file.return_value.download_as_bytearray
    download.side_effect = [TimedOut(), TimedOut(), bytearray(b"ogg-data")]
    await receive(adapter, message(adapter._bot))
    await clock.advance(30)
    assert_voice(adapter)
    adapter.send.assert_not_awaited()
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_outgoing_timeout_then_delivers_without_failure_notice(rig, tmp_path):
    adapter, clock = rig
    audio = tmp_path / "speech.ogg"
    audio.write_bytes(b"ogg-data")
    adapter._bot.send_voice.side_effect = [TimedOut(), SimpleNamespace(message_id=103)]
    result = await adapter.send_voice("100", str(audio))
    audio.unlink()  # GatewayRunner removes auto-TTS outputs as soon as send returns.
    await clock.advance(30)
    assert adapter._bot.send_voice.await_count == 2
    assert result.success is True
    assert result.message_id is None
    assert result.raw_response == {"voice_retry_scheduled": True}
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_outage_35_minutes_notices_once_and_one_lost_marker(rig, caplog):
    adapter, clock = rig
    adapter._bot.get_file.side_effect = TimedOut()
    delivered = []
    async def send(*args, **kwargs):
        if clock.now < 180:
            return SendResult(success=False, error="Timed out")
        delivered.append(args[1])
        return SendResult(success=True, message_id="notice")
    adapter.send.side_effect = send
    with caplog.at_level(logging.INFO):
        await receive(adapter, message(adapter._bot))
        await clock.advance(35 * 60)
    sent = [c for c in adapter.send.await_args_list if c.args[1].startswith("Telegram are")]
    # The first T1 attempt fails; only its later successful delivery counts.
    assert len(sent) == 2
    assert sum(text.startswith("Telegram are") for text in delivered) == 1
    assert sum(text.startswith("Nu reușesc") for text in delivered) == 1
    texts = [c.args[1] for c in adapter.send.await_args_list]
    assert sum("Nu reușesc" in text for text in texts) == 1
    assert all("Couldn't" not in text for text in texts)
    assert lost_lines(caplog) == [
        "[Telegram] media-retry LOST kind=voice chat=100 message=42 "
        "sent_at=2026-11-08T09:20:57Z reason=window"
    ]
    adapter.handle_message.assert_not_awaited()
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_minute_ten_recovery_has_late_note_and_no_t2(rig, caplog):
    adapter, clock = rig
    original = adapter._bot.get_file.return_value
    async def get_file(*args, **kwargs):
        if clock.now < 600:
            raise TimedOut()
        return original
    adapter._bot.get_file.side_effect = get_file
    await receive(adapter, message(adapter._bot))
    await clock.advance(700)
    event = assert_voice(adapter)
    assert "11:20" in event.text
    assert "arrived late because Telegram was unavailable" in event.text
    assert "does not need to resend" in event.text
    assert adapter.send.await_count == 1
    assert not lost_lines(caplog)


@pytest.mark.asyncio
async def test_retry_uses_replacement_bot_after_disconnect(rig):
    adapter, clock = rig
    old = adapter._bot
    old.get_file.side_effect = TimedOut()
    await receive(adapter, message(old))
    adapter._set_fatal_error("network", "Timed out", retryable=True)
    await adapter.disconnect()
    await clock.advance(5)  # No bot during reconnect is another transient failure.
    adapter._bot = make_bot()
    adapter._mark_connected()
    await clock.advance(30)
    assert_voice(adapter)
    adapter._bot.get_file.assert_awaited_with("file-42")


@pytest.mark.asyncio
async def test_lost_document_dispatches_caption_and_give_up_note(rig, caplog):
    adapter, clock = rig
    adapter._bot.get_file.side_effect = TimedOut()
    await receive(adapter, message(adapter._bot, "document", caption="Please read this"))
    await clock.advance(35 * 60)
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert "Please read this" in event.text
    assert "could not be downloaded after 30 minutes" in event.text
    assert "Do not ask again." in event.text
    assert sum("Nu reușesc" in c.args[1] for c in adapter.send.await_args_list) == 1
    assert len(lost_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_bad_request_keeps_immediate_failure_behavior(rig):
    adapter, clock = rig
    adapter._bot.get_file.side_effect = BadRequest("File unavailable")
    await receive(adapter, message(adapter._bot))
    await clock.advance(60)
    adapter.handle_message.assert_awaited_once()
    assert "could not be downloaded (BadRequest)" in adapter.handle_message.await_args.args[0].text
    adapter._bot.send_message.assert_awaited_once()
    assert "Couldn't download your voice message" in adapter._bot.send_message.await_args.kwargs["text"]
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_marks_each_pending_message_lost_once_without_notice(rig, caplog):
    adapter, clock = rig
    adapter._bot.get_file.side_effect = TimedOut()
    await receive(adapter, message(adapter._bot, message_id=42))
    await receive(adapter, message(adapter._bot, message_id=43))
    await receive(adapter, message(adapter._bot, message_id=42))
    await clock.advance(5)
    await adapter.disconnect()
    await adapter.disconnect()
    assert len(lost_lines(caplog)) == 2
    assert all(line.endswith("reason=shutdown") for line in lost_lines(caplog))
    adapter.send.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_download_transfers_to_new_connected_adapter(rig):
    adapter, clock = rig
    adapter._bot.get_file.side_effect = TimedOut()
    await receive(adapter, message(adapter._bot))
    adapter._set_fatal_error("network", "Timed out", retryable=True)
    await adapter.disconnect()
    replacement = TelegramAdapter(adapter.config)
    replacement._bot = make_bot()
    replacement._media_retry_now = lambda: clock.now
    replacement._media_retry_sleep = clock.sleep
    replacement.handle_message = AsyncMock()
    replacement._mark_connected()
    try:
        await clock.advance(30)
        assert_voice(replacement)
        adapter.handle_message.assert_not_awaited()
        replacement._bot.get_file.assert_awaited_with("file-42")
    finally:
        await replacement.disconnect()


@pytest.mark.asyncio
async def test_t2_stops_after_four_failed_sends(rig, caplog):
    adapter, clock = rig
    adapter._bot.get_file.side_effect = TimedOut()
    adapter.send.return_value = SendResult(success=False, error="Timed out")
    await receive(adapter, message(adapter._bot))
    await clock.advance(45 * 60)
    count = adapter.send.await_count
    assert sum("Nu reușesc" in c.args[1] for c in adapter.send.await_args_list) == 4
    await clock.advance(60 * 60)
    assert adapter.send.await_count == count
    assert len(lost_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_retry_after_respects_server_delay(rig):
    adapter, clock = rig
    adapter._bot.get_file.side_effect = [RetryAfter(40), adapter._bot.get_file.return_value]
    await receive(adapter, message(adapter._bot))
    await clock.advance(39)
    assert adapter._bot.get_file.await_count == 1
    adapter.handle_message.assert_not_awaited()
    await clock.advance(1)
    assert_voice(adapter)
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["audio", "video", "document", "photo", "image_document"])
@pytest.mark.parametrize("album", [False, True])
async def test_all_media_kinds_recover_through_original_dispatch(rig, kind, album):
    import io
    from PIL import Image

    adapter, clock = rig
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, format="PNG")
    data = image.getvalue() if kind in {"photo", "image_document"} else b"media-data"
    adapter._bot.get_file.return_value.file_path = "file.png" if kind == "photo" else "file.mp4"
    adapter._bot.get_file.return_value.download_as_bytearray.side_effect = [TimedOut(), bytearray(data)]
    msg = message(adapter._bot, "document" if kind == "image_document" else kind,
                  caption="Caption", **({"media_group_id": "album-1"} if album else {}))
    if kind == "image_document":
        msg = Message.de_json({**msg.to_dict(), "document": {
            "file_id": "file-42", "file_unique_id": "unique-42", "file_size": len(data),
            "file_name": "photo.png", "mime_type": "image/png",
        }}, adapter._bot)
    adapter._media_batch_delay_seconds = 0
    adapter.MEDIA_GROUP_WAIT_SECONDS = 0
    await receive(adapter, msg)
    await clock.advance(30)
    for _ in range(10):
        await asyncio.sleep(0)
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "Caption"
    assert len(event.media_urls) == 1
    assert Path(event.media_urls[0]).exists()
    assert event.media_types[0].startswith({
        "audio": "audio/", "video": "video/", "document": "application/",
        "photo": "image/", "image_document": "image/",
    }[kind])
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_notices_keep_original_reply_and_dm_topic_metadata(rig):
    adapter, clock = rig
    adapter._bot.get_file.side_effect = TimedOut()
    msg = message(adapter._bot, message_thread_id=77, is_topic_message=True)
    await receive(adapter, msg)
    await clock.advance(35 * 60)
    assert adapter.send.await_count == 2
    for call in adapter.send.await_args_list:
        assert call.args[0] == "100"
        assert call.kwargs["reply_to"] == "42"
        metadata = call.kwargs["metadata"]
        assert metadata["thread_id"] == "77"
        assert metadata["telegram_reply_to_message_id"] == "42"
        assert metadata["direct_messages_topic_id"] == "77"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,method", [(".ogg", "send_voice"), (".opus", "send_voice"),
                                           (".mp3", "send_audio"), (".m4a", "send_audio")])
async def test_outgoing_caption_arrives_now_and_retry_owns_file(rig, tmp_path, suffix, method):
    adapter, clock = rig
    audio = tmp_path / ("speech" + suffix)
    audio.write_bytes(b"ogg-data")
    uploads = getattr(adapter._bot, method)
    contents = []
    async def upload(**kwargs):
        stream = kwargs["voice" if method == "send_voice" else "audio"]
        contents.append(stream.read())
        if len(contents) == 1:
            raise TimedOut()
        assert kwargs["caption"] is None
        assert kwargs["message_thread_id"] == 77
        return SimpleNamespace(message_id=103)
    uploads.side_effect = upload
    metadata = {"thread_id": "77"}
    result = await adapter.send_voice("100", str(audio), caption="Răspuns", reply_to="42", metadata=metadata)
    assert result.success
    adapter.send.assert_awaited_once_with("100", "Răspuns", reply_to="42", metadata=metadata)
    copies = list((adapter._hermes_home / "cache" / "voice-retry").iterdir())
    assert len(copies) == 1
    assert copies[0].read_bytes() == b"ogg-data"
    audio.unlink()
    await clock.advance(30)
    assert contents == [b"ogg-data", b"ogg-data"]
    assert not copies[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_outgoing_give_up_removes_copy_without_human_notice(rig, tmp_path, caplog, shutdown):
    adapter, clock = rig
    audio = tmp_path / "speech.ogg"
    audio.write_bytes(b"ogg-data")
    adapter._bot.send_voice.side_effect = TimedOut()
    result = await adapter.send_voice("100", str(audio))
    assert result.success
    copies = list((adapter._hermes_home / "cache" / "voice-retry").iterdir())
    if shutdown:
        await adapter.disconnect()
    else:
        await clock.advance(35 * 60)
    assert all(not path.exists() for path in copies)
    assert sum("voice-retry gave up" in r.getMessage() for r in caplog.records) == 1
    assert not lost_lines(caplog)
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_outgoing_bad_request_still_uses_base_notice(rig, tmp_path):
    adapter, clock = rig
    audio = tmp_path / "speech.ogg"
    audio.write_bytes(b"ogg-data")
    adapter._bot.send_voice.side_effect = BadRequest("Unsupported audio")
    await adapter.send_voice("100", str(audio))
    await clock.advance(30)
    assert adapter._bot.send_voice.await_count == 1
    assert "Couldn't deliver the audio attachment" in adapter.send.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_outgoing_pending_upload_uses_new_bot(rig, tmp_path):
    adapter, clock = rig
    audio = tmp_path / "speech.ogg"
    audio.write_bytes(b"ogg-data")
    old = adapter._bot
    old.send_voice.side_effect = TimedOut()
    await adapter.send_voice("100", str(audio))
    adapter._bot = None
    await clock.advance(6)
    adapter._bot = make_bot()
    await clock.advance(30)
    adapter._bot.send_voice.assert_awaited_once()
    old.send_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_shutdown_cancels_even_retryable_fatal_adapter(rig, caplog):
    adapter, clock = rig
    adapter._bot.get_file.side_effect = TimedOut()
    await receive(adapter, message(adapter._bot))
    adapter._set_fatal_error("network", "Timed out", retryable=True)
    await adapter.cancel_background_tasks()
    await adapter.disconnect()
    await clock.advance(3600)
    assert len(lost_lines(caplog)) == 1
    assert lost_lines(caplog)[0].endswith("reason=shutdown")
    adapter.send.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["connect", "read", "protocol", "os", "502"])
async def test_transport_errors_recover_without_notices(rig, error_kind):
    import httpx

    adapter, clock = rig
    error = {
        "connect": httpx.ConnectError("connection lost"),
        "read": httpx.ReadTimeout("read failed"),
        "protocol": httpx.RemoteProtocolError("connection closed"),
        "os": OSError("network unavailable"),
        "502": RuntimeError("Bad Gateway (502)"),
    }[error_kind]
    adapter._bot.get_file.side_effect = [error, adapter._bot.get_file.return_value]
    await receive(adapter, message(adapter._bot))
    await clock.advance(30)
    assert_voice(adapter)
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_notice_after_slow_initial_failure_and_log_redaction(rig, caplog):
    adapter, clock = rig
    secret = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    original = adapter._bot.get_file.return_value
    async def first_failure(*args, **kwargs):
        clock.now = 125
        raise TimedOut(f"https://api.telegram.org/file/bot{secret}/voice/file.ogg")
    adapter._bot.get_file.side_effect = first_failure
    await receive(adapter, message(adapter._bot))
    adapter._bot.get_file.side_effect = None
    adapter._bot.get_file.return_value = original
    await clock.advance(30)
    assert_voice(adapter)
    adapter.send.assert_awaited_once()
    assert secret not in caplog.text
    assert "api.telegram.org/file/" not in caplog.text
