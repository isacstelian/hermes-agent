"""Behavior tests for the explicitly configured STT fallback provider."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tools import transcription_tools


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"fake audio data")
    return str(path)


@pytest.fixture(autouse=True)
def provider_credentials(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-test-key")
    monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "openai-test-key")


def test_default_config_disables_cross_provider_fallback():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["stt"]["fallback_provider"] == ""


def test_elevenlabs_quota_failure_uses_explicit_openai_fallback(
    audio_file, monkeypatch, caplog,
):
    config = {
        "provider": "elevenlabs",
        "fallback_provider": "openai",
        "prompt": "Magic, Fillout",
        "elevenlabs": {"model_id": "scribe_v2"},
        "openai": {"model": "gpt-transcribe"},
    }
    hook_calls = []

    def invoke_hook(_name, **kwargs):
        hook_calls.append(kwargs)
        return []

    elevenlabs_response = MagicMock(status_code=401)
    elevenlabs_response.json.return_value = {
        "detail": {"message": "quota exhausted: 0 credits remaining"}
    }
    openai_client = MagicMock()
    openai_client.audio.transcriptions.create.return_value = SimpleNamespace(
        text="mesajul vocal"
    )

    with caplog.at_level("WARNING", logger="tools.transcription_tools"), \
         patch.object(transcription_tools, "_load_stt_config", return_value=config), \
         patch.object(transcription_tools, "_HAS_OPENAI", True), \
         patch.object(
             transcription_tools, "_trim_silence_for_cloud_stt", return_value=None
         ) as trim, \
         patch("requests.post", return_value=elevenlabs_response) as elevenlabs_post, \
         patch("openai.OpenAI", return_value=openai_client), \
         patch("hermes_cli.plugins.has_hook", return_value=True), \
         patch("hermes_cli.plugins.invoke_hook", side_effect=invoke_hook):
        result = transcription_tools.transcribe_audio(
            audio_file, source="gateway"
        )

    assert result == {
        "success": True,
        "transcript": "mesajul vocal",
        "provider": "openai",
    }
    assert elevenlabs_post.call_args.kwargs["data"]["model_id"] == "scribe_v2"
    assert openai_client.audio.transcriptions.create.call_args.kwargs["model"] == (
        "gpt-transcribe"
    )
    assert [call["provider"] for call in hook_calls] == ["elevenlabs", "openai"]
    assert all(call["source"] == "gateway" for call in hook_calls)
    assert trim.call_count == 2
    assert "quota exhausted: 0 credits remaining" in caplog.text


def test_cloud_fallback_is_not_used_without_explicit_opt_in(audio_file):
    config = {
        "provider": "elevenlabs",
        "fallback_provider": "",
        "openai": {"model": "gpt-transcribe"},
    }
    primary_result = {
        "success": False,
        "transcript": "",
        "error": "ElevenLabs STT API error (HTTP 401): quota exhausted",
    }

    with patch.object(transcription_tools, "_load_stt_config", return_value=config), \
         patch.object(transcription_tools, "_transcribe_elevenlabs", return_value=primary_result), \
         patch.object(transcription_tools, "_transcribe_openai") as fallback, \
         patch.object(transcription_tools, "_trim_silence_for_cloud_stt", return_value=None):
        result = transcription_tools.transcribe_audio(audio_file)

    assert result == primary_result
    fallback.assert_not_called()


def test_same_provider_fallback_is_rejected_without_retry(audio_file):
    config = {
        "provider": "elevenlabs",
        "fallback_provider": " ELEVENLABS ",
    }
    primary = MagicMock(
        return_value={
            "success": False,
            "transcript": "",
            "error": "HTTP 401",
        }
    )

    with patch.object(transcription_tools, "_load_stt_config", return_value=config), \
         patch.object(transcription_tools, "_transcribe_elevenlabs", primary), \
         patch.object(transcription_tools, "_trim_silence_for_cloud_stt", return_value=None):
        result = transcription_tools.transcribe_audio(audio_file)

    assert primary.call_count == 1
    assert result["success"] is False
    assert "Primary STT provider 'elevenlabs' failed: HTTP 401" in result["error"]
    assert "same as the primary provider" in result["error"]


def test_unavailable_configured_fallback_keeps_primary_and_config_errors(
    audio_file, monkeypatch,
):
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY")
    config = {
        "provider": "elevenlabs",
        "fallback_provider": "openai",
    }

    with patch.object(transcription_tools, "_load_stt_config", return_value=config), \
         patch.object(transcription_tools, "_HAS_OPENAI", True), \
         patch.object(
             transcription_tools,
             "_has_openai_audio_backend",
             return_value=False,
         ), \
         patch.object(
             transcription_tools,
             "_transcribe_elevenlabs",
             return_value={"success": False, "transcript": "", "error": "HTTP 401"},
         ), \
         patch.object(transcription_tools, "_transcribe_openai") as fallback, \
         patch.object(transcription_tools, "_trim_silence_for_cloud_stt", return_value=None):
        result = transcription_tools.transcribe_audio(audio_file)

    fallback.assert_not_called()
    assert "Primary STT provider 'elevenlabs' failed: HTTP 401" in result["error"]
    assert "fallback_provider='openai'" in result["error"]
    assert "unavailable" in result["error"]


def test_both_provider_errors_are_returned_and_logged(audio_file, caplog):
    config = {
        "provider": "elevenlabs",
        "fallback_provider": "openai",
        "openai": {"model": "gpt-transcribe"},
    }

    with caplog.at_level("WARNING", logger="tools.transcription_tools"), \
         patch.object(transcription_tools, "_load_stt_config", return_value=config), \
         patch.object(transcription_tools, "_HAS_OPENAI", True), \
         patch.object(
             transcription_tools,
             "_transcribe_elevenlabs",
             return_value={
                 "success": False,
                 "transcript": "",
                 "error": "HTTP 401 quota exhausted",
             },
         ), \
         patch.object(
             transcription_tools,
             "_transcribe_openai",
             return_value={
                 "success": False,
                 "transcript": "",
                 "error": "OpenAI HTTP 429 rate limited",
             },
         ), \
         patch.object(transcription_tools, "_trim_silence_for_cloud_stt", return_value=None):
        result = transcription_tools.transcribe_audio(audio_file)

    assert result["provider"] == "openai"
    assert result["primary_provider"] == "elevenlabs"
    assert result["fallback_provider"] == "openai"
    assert result["primary_error"] == "HTTP 401 quota exhausted"
    assert result["fallback_error"] == "OpenAI HTTP 429 rate limited"
    assert "HTTP 401 quota exhausted" in result["error"]
    assert "OpenAI HTTP 429 rate limited" in result["error"]
    assert "HTTP 401 quota exhausted" in caplog.text
    assert "OpenAI HTTP 429 rate limited" in caplog.text


def test_cloud_fallback_reapplies_caf_conversion_and_size_cap(tmp_path):
    caf_path = tmp_path / "voice.caf"
    caf_path.write_bytes(b"caff" * 20)
    wav_path = tmp_path / "voice.wav"
    wav_path.write_bytes(b"RIFF" * 20)
    config = {
        "provider": "local",
        "fallback_provider": "openai",
        "openai": {"model": "gpt-transcribe"},
    }
    local = MagicMock(
        return_value={"success": False, "transcript": "", "error": "local failed"}
    )
    openai = MagicMock(
        return_value={"success": True, "transcript": "salut", "provider": "openai"}
    )

    with patch.object(transcription_tools, "_load_stt_config", return_value=config), \
         patch.object(transcription_tools, "_HAS_FASTER_WHISPER", True), \
         patch.object(transcription_tools, "_HAS_OPENAI", True), \
         patch.object(transcription_tools, "_transcribe_local", local), \
         patch.object(transcription_tools, "_transcribe_openai", openai), \
         patch.object(
             transcription_tools, "_convert_caf_to_wav", return_value=str(wav_path)
         ) as convert, \
         patch.object(transcription_tools, "_trim_silence_for_cloud_stt", return_value=None):
        result = transcription_tools.transcribe_audio(str(caf_path))

    assert result["success"] is True
    assert local.call_args.args[0] == str(caf_path)
    assert openai.call_args.args[:2] == (str(wav_path), "gpt-transcribe")
    convert.assert_called_once_with(str(caf_path))


def test_oversized_file_cannot_bypass_cloud_cap_through_local_primary(tmp_path):
    path = tmp_path / "oversized.wav"
    with path.open("wb") as audio:
        audio.seek(transcription_tools.MAX_FILE_SIZE)
        audio.write(b"\0")
    config = {
        "provider": "local",
        "fallback_provider": "openai",
    }

    with patch.object(transcription_tools, "_load_stt_config", return_value=config), \
         patch.object(transcription_tools, "_HAS_FASTER_WHISPER", True), \
         patch.object(transcription_tools, "_HAS_OPENAI", True), \
         patch.object(
             transcription_tools,
             "_transcribe_local",
             return_value={"success": False, "transcript": "", "error": "local failed"},
         ), \
         patch.object(transcription_tools, "_transcribe_openai") as openai:
        result = transcription_tools.transcribe_audio(str(path))

    openai.assert_not_called()
    assert result["success"] is False
    assert "too large" in result["fallback_error"]
