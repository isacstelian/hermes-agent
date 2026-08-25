"""
Tests for Telegram voice and audio-file STT routing.

Telegram distinguishes three kinds of audio payloads:
  - message.voice  → Opus/OGG voice message  → STT pipeline
  - message.audio  → audio file attachment   → STT pipeline
  - message.document (audio mime) → generic file route

These tests confirm that:
  1. MessageType.VOICE events still flow through the STT pipeline.
  2. MessageType.AUDIO events also flow through the STT pipeline.
"""

from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
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


# ---------------------------------------------------------------------------
# 4. Telegram gateway: msg.audio → MessageType.AUDIO (not VOICE)
# ---------------------------------------------------------------------------
