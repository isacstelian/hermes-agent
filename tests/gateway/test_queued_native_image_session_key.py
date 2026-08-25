import base64
import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource
from gateway.session_context import (
    clear_session_vars,
    current_attachments_meta,
    set_current_message_context,
    set_session_vars,
)


_ONE_BY_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6L2ioAAAAASUVORK5CYII="
)


class CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []
        self.typing = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="sent-1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        self.typing.append({"chat_id": chat_id, "metadata": metadata})

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class CaptureQueuedNativeImageAgent:
    calls = []

    def __init__(self, **kwargs):
        self.tools = []
        self.tool_progress_callback = kwargs.get("tool_progress_callback")

    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).calls.append(message)
        return {
            "final_response": f"done-{len(type(self).calls)}",
            "messages": [],
            "api_calls": 1,
        }


class CaptureQueuedAttachmentAgent(CaptureQueuedNativeImageAgent):
    attachment_meta = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).attachment_meta.append(current_attachments_meta())
        return super().run_conversation(message, conversation_history, task_id)


class CapturePendingSteerAttachmentAgent(CaptureQueuedAttachmentAgent):
    def run_conversation(self, message, conversation_history=None, task_id=None):
        result = super().run_conversation(message, conversation_history, task_id)
        if len(type(self).calls) == 1:
            result["pending_steer"] = "steered follow-up"
        return result


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._decide_image_input_mode = lambda **_kw: "native"
    return runner


@pytest.mark.asyncio
async def test_queued_followup_uses_pending_event_session_key_for_native_images(monkeypatch, tmp_path):
    CaptureQueuedNativeImageAgent.calls = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CaptureQueuedNativeImageAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)

    image_path = tmp_path / "queued-image.png"
    image_path.write_bytes(_ONE_BY_ONE_PNG)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
    )
    pending_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    adapter._pending_messages["agent:main:telegram:group:-1001"] = MessageEvent(
        text="describe this",
        message_type=MessageType.PHOTO,
        source=pending_source,
        media_urls=[str(image_path)],
        media_types=["image/png"],
        message_id="queued-1",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-native-image-followup",
        session_key="agent:main:telegram:group:-1001",
    )

    assert result["final_response"] == "done-2"
    assert len(CaptureQueuedNativeImageAgent.calls) == 2
    queued_message = CaptureQueuedNativeImageAgent.calls[1]
    assert isinstance(queued_message, list)
    assert queued_message[0]["type"] == "text"
    assert queued_message[0]["text"].startswith("describe this")
    assert any(part.get("type") == "image_url" for part in queued_message)


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_has_attachment", [False, True])
async def test_queued_followup_rebinds_current_attachment_and_message_id(
    monkeypatch, tmp_path, queued_has_attachment
):
    CaptureQueuedAttachmentAgent.calls = []
    CaptureQueuedAttachmentAgent.attachment_meta = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CaptureQueuedAttachmentAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm")
    session_key = "agent:main:telegram:dm:chat-1"
    first_path = tmp_path / "first.pdf"
    queued_path = tmp_path / "queued.pdf"
    first_path.write_bytes(b"first")
    queued_path.write_bytes(b"queued")
    adapter._pending_messages[session_key] = MessageEvent(
        text="use queued",
        message_type=MessageType.DOCUMENT,
        source=source,
        media_urls=[str(queued_path)] if queued_has_attachment else [],
        media_types=["application/pdf"] if queued_has_attachment else [],
        message_id="queued-2",
    )
    tokens = set_session_vars(platform="telegram", message_id="first-1")
    set_current_message_context(
        "first-1", [str(first_path)], ["application/pdf"]
    )

    try:
        await runner._run_agent(
            message="first turn",
            context_prompt="",
            history=[],
            source=source,
            session_id="sess-queued-attachment",
            session_key=session_key,
        )
    finally:
        clear_session_vars(tokens)

    first, queued = CaptureQueuedAttachmentAgent.attachment_meta
    assert first["ai.hermes/current-attachments"]["message_id"] == "first-1"
    assert first["ai.hermes/current-attachments"]["files"][0]["path"] == str(first_path)
    if queued_has_attachment:
        assert queued["ai.hermes/current-attachments"]["message_id"] == "queued-2"
        assert queued["ai.hermes/current-attachments"]["files"][0]["path"] == str(queued_path)
    else:
        assert queued is None


@pytest.mark.asyncio
async def test_pending_steer_clears_current_attachment_and_message_id(monkeypatch, tmp_path):
    CapturePendingSteerAttachmentAgent.calls = []
    CapturePendingSteerAttachmentAgent.attachment_meta = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CapturePendingSteerAttachmentAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm")
    first_path = tmp_path / "first.pdf"
    first_path.write_bytes(b"first")
    tokens = set_session_vars(platform="telegram", message_id="first-1")
    set_current_message_context("first-1", [str(first_path)], ["application/pdf"])

    try:
        await runner._run_agent(
            message="first turn",
            context_prompt="",
            history=[],
            source=source,
            session_id="sess-pending-steer-attachment",
            session_key="agent:main:telegram:dm:chat-1",
        )
    finally:
        clear_session_vars(tokens)

    first, steered = CapturePendingSteerAttachmentAgent.attachment_meta
    assert first["ai.hermes/current-attachments"]["message_id"] == "first-1"
    assert first["ai.hermes/current-attachments"]["files"][0]["path"] == str(first_path)
    assert steered is None
