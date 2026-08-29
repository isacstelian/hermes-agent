"""
Tests for Telegram voice and audio-file STT routing.

Telegram distinguishes three kinds of audio payloads:
  - message.voice  → Opus/OGG voice message  → STT pipeline
  - message.audio  → audio file attachment   → STT pipeline
  - message.document (audio mime) → promoted to audio and sent through STT

These tests confirm that:
  1. MessageType.VOICE events still flow through the STT pipeline.
  2. MessageType.AUDIO events also flow through the STT pipeline.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _make_runner(stt_enabled: bool = True) -> "GatewayRunner":  # type: ignore[name-defined]
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=stt_enabled)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    return runner


def _voice_event(path: str = "/tmp/voice.ogg") -> MessageEvent:
    return MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm"),
        media_urls=[path],
        media_types=["audio/ogg"],
    )


def _audio_event(path: str = "/tmp/song.mp3") -> MessageEvent:
    return MessageEvent(
        text="",
        message_type=MessageType.AUDIO,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm"),
        media_urls=[path],
        media_types=["audio/mpeg"],
    )


# ---------------------------------------------------------------------------
# 1. VOICE still goes through STT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voice_message_still_transcribed():
    """MessageType.VOICE must still be sent through _enrich_message_with_transcription."""
    runner = _make_runner(stt_enabled=True)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm")
    event = _voice_event("/tmp/voice.ogg")

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "hello world", "provider": "whisper"},
    ) as mock_transcribe:
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    mock_transcribe.assert_called_once_with("/tmp/voice.ogg", None, "gateway")
    # The transcript passes through as a plain quoted line — no "voice message"
    # meta-commentary in the LLM-visible prompt.
    assert "hello world" in result


# ---------------------------------------------------------------------------
# 2. AUDIO file attachment goes through STT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_attachment_context_note_format():
    """Audio file attachments should be transcribed before reaching the agent."""
    runner = _make_runner(stt_enabled=True)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm")
    event = _audio_event("/tmp/cache_12345_my_song.mp3")

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "song transcript", "provider": "openai"},
    ) as mock_transcribe:
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    mock_transcribe.assert_called_once_with("/tmp/cache_12345_my_song.mp3", None, "gateway")
    assert "song transcript" in result
    assert "audio file attachment" not in result.lower()


# ---------------------------------------------------------------------------
# 3. STT disabled still results in no transcription for audio file attachments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_attachment_stt_disabled_keeps_audio_context():
    runner = _make_runner(stt_enabled=False)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm")
    event = _audio_event("/tmp/song.m4a")

    with patch("tools.transcription_tools.transcribe_audio") as mock_transcribe:
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    mock_transcribe.assert_not_called()
    assert "audio message" in result
    assert "/tmp/song.m4a" in result


# ---------------------------------------------------------------------------
# 4. Telegram gateway: msg.audio → MessageType.AUDIO (not VOICE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_native_m4a_preserves_extension_and_mime(tmp_path, monkeypatch):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setattr("gateway.platforms.base.AUDIO_CACHE_DIR", tmp_path)
    audio_bytes = b"\x00\x00\x00\x1cftypM4A " + b"\x00" * 64
    file_obj = SimpleNamespace(
        file_path="audio/note.m4a",
        download_as_bytearray=AsyncMock(return_value=bytearray(audio_bytes)),
    )
    audio = SimpleNamespace(
        file_name="note.m4a",
        mime_type="audio/mp4",
        file_size=len(audio_bytes),
        get_file=AsyncMock(return_value=file_obj),
    )
    msg = MagicMock()
    msg.message_id = 42
    msg.text = ""
    msg.caption = None
    msg.date = None
    msg.photo = None
    msg.video = None
    msg.audio = audio
    msg.voice = None
    msg.sticker = None
    msg.document = None
    msg.media_group_id = None
    msg.message_thread_id = None
    msg.chat = SimpleNamespace(id=1, type="private", title=None, full_name="Test")
    msg.from_user = SimpleNamespace(id=1, full_name="Test")

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter.handle_message = AsyncMock()
    adapter._is_callback_user_authorized = lambda user_id, **_kw: True

    await adapter._handle_media_message(
        SimpleNamespace(message=msg, update_id=1),
        MagicMock(),
    )

    event = adapter.handle_message.await_args.args[0]
    assert event.message_type == MessageType.AUDIO
    assert os.path.splitext(event.media_urls[0])[1] == ".m4a"
    assert event.media_types == ["audio/mp4"]


@pytest.mark.asyncio
async def test_pending_audio_event_is_transcribed_once():
    runner = _make_runner(stt_enabled=True)
    event = _audio_event("/tmp/note.m4a")

    with patch.object(
        runner,
        "_enrich_message_with_transcription",
        AsyncMock(return_value=('"pending transcript"', ["pending transcript"])),
    ) as mock_enrich:
        first = await runner._transcribe_pending_audio_event_once(event)
        second = await runner._transcribe_pending_audio_event_once(event)

    assert first == second
    mock_enrich.assert_awaited_once_with("", ["/tmp/note.m4a"])
