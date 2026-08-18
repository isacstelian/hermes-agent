"""Tests for fetching MEDIA files out of a terminal backend (#466, #75065).

Covers the remote-path credential denylist, the size cap, and the
transparent fetch through ``filter_media_delivery_paths_with_drops``: a
MEDIA path that exists only inside the agent's sandbox is pulled into the
document cache and delivered from there, while a denied path stays denied
and a failed fetch still produces the user-facing notice.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.media_fetch import (
    MEDIA_FETCH_MAX_BYTES_ENV,
    fetch_remote_media,
    media_fetch_max_bytes,
    remote_path_is_denied,
)
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MEDIA_DROP_MISSING,
    MessageEvent,
    SendResult,
)
from gateway.session import SessionSource, build_session_key
from tools.environments.base import FileFetchError


class _FakeRemoteEnv:
    """Duck-typed environment serving files from an in-memory dict."""

    def __init__(self, files=None, home="/root", links=None):
        self.files = files or {}
        self.links = links or {}
        self._remote_home = home
        self.fetched = []

    @property
    def remote_home(self):
        return self._remote_home

    def fetch_realpath(self, remote_path):
        return self.links.get(remote_path, remote_path)

    def fetch_file_size(self, remote_path):
        data = self.files.get(remote_path)
        return None if data is None else len(data)

    def fetch_file(self, remote_path, local_dest):
        data = self.files.get(remote_path)
        if data is None:
            raise FileFetchError(f"{remote_path!r} not found")
        self.fetched.append(remote_path)
        with open(local_dest, "wb") as f:
            f.write(data)


@pytest.fixture()
def remote_backend(tmp_path, monkeypatch):
    """Activate a fake docker backend with the document cache in tmp_path."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )

    def _install(env):
        monkeypatch.setattr(
            "tools.terminal_tool.get_active_env", lambda task_id: env
        )
        return env

    return _install


class TestRemotePathDenylist:
    @pytest.mark.parametrize("path", [
        "/etc/passwd",
        "/proc/self/environ",
        "/root/.ssh/id_rsa",
        "/root/.aws/credentials",
        "/root/.hermes/.env",
        "/root/.hermes/auth.json",
        "/root/.hermes/mcp-tokens/github.json",
        "/root/work/../.ssh/id_rsa",
    ])
    def test_denied_with_known_home(self, path):
        assert remote_path_is_denied(path, "/root") is True

    @pytest.mark.parametrize("path", [
        "/root/raport.xlsx",
        "/workspace/build/output.zip",
        "/tmp/chart.png",
        "/root/.hermes/skills/notes.md",
    ])
    def test_allowed_with_known_home(self, path):
        assert remote_path_is_denied(path, "/root") is False

    def test_container_home_exception(self):
        """/root is a denied system prefix, but it IS the container's home."""
        assert remote_path_is_denied("/root/raport.xlsx", "/root") is False
        assert remote_path_is_denied("/root/.ssh/id_rsa", "/root") is True
        assert remote_path_is_denied("/root/raport.xlsx", "/home/worker") is True

    def test_unknown_home_is_conservative(self):
        assert remote_path_is_denied("/data/.ssh/id_rsa", None) is True
        assert remote_path_is_denied("/srv/app/report.pdf", None) is False

    def test_relative_path_denied(self):
        assert remote_path_is_denied("workspace/report.pdf", "/root") is True


class TestFetchRemoteMedia:
    def test_fetches_container_only_file_into_the_cache(self, remote_backend):
        env = remote_backend(_FakeRemoteEnv({"/root/raport.xlsx": b"xlsx bytes"}))

        local, reason = fetch_remote_media("/root/raport.xlsx", "session-1")

        assert reason is None
        assert local and open(local, "rb").read() == b"xlsx bytes"
        assert env.fetched == ["/root/raport.xlsx"]
        assert "raport.xlsx" in local

    def test_missing_in_backend_reports_reason(self, remote_backend):
        remote_backend(_FakeRemoteEnv({}))

        local, reason = fetch_remote_media("/root/gone.xlsx", "session-1")

        assert local is None
        assert "not found" in reason

    def test_credential_path_is_never_fetched(self, remote_backend):
        env = remote_backend(_FakeRemoteEnv({"/root/.ssh/id_rsa": b"KEY"}))

        local, reason = fetch_remote_media("/root/.ssh/id_rsa", "session-1")

        assert local is None
        assert reason == "the path is not allowed for delivery"
        assert env.fetched == []

    def test_symlink_to_credential_is_rejected(self, remote_backend):
        env = remote_backend(_FakeRemoteEnv(
            files={"/root/.ssh/id_rsa": b"KEY"},
            links={"/workspace/innocent.pdf": "/root/.ssh/id_rsa"},
        ))

        local, reason = fetch_remote_media("/workspace/innocent.pdf", "session-1")

        assert local is None
        assert reason == "the path is not allowed for delivery"
        assert env.fetched == []

    def test_oversized_file_is_refused(self, remote_backend, monkeypatch):
        env = remote_backend(_FakeRemoteEnv({"/root/big.zip": b"x" * 5000}))
        monkeypatch.setenv(MEDIA_FETCH_MAX_BYTES_ENV, "1024")

        local, reason = fetch_remote_media("/root/big.zip", "session-1")

        assert local is None
        assert "delivery limit" in reason
        assert env.fetched == []

    def test_no_active_session_reports_reason(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setattr("tools.terminal_tool.get_active_env", lambda task_id: None)

        local, reason = fetch_remote_media("/root/raport.xlsx", "session-1")

        assert local is None
        assert "no active docker terminal session" in reason

    def test_local_backend_does_not_fetch(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")

        local, reason = fetch_remote_media("/root/raport.xlsx", "session-1")

        assert local is None
        assert reason == "no remote terminal backend is active"

    def test_size_cap_default_and_override(self, monkeypatch):
        monkeypatch.delenv(MEDIA_FETCH_MAX_BYTES_ENV, raising=False)
        assert media_fetch_max_bytes() == 50 * 1024 * 1024
        monkeypatch.setenv(MEDIA_FETCH_MAX_BYTES_ENV, "2048")
        assert media_fetch_max_bytes() == 2048
        monkeypatch.setenv(MEDIA_FETCH_MAX_BYTES_ENV, "nonsense")
        assert media_fetch_max_bytes() == 50 * 1024 * 1024


class TestFilterUsesTheFetch:
    """The incident shape: MEDIA:/root/raport.xlsx from a sandboxed agent."""

    def test_container_path_becomes_a_deliverable_local_file(self, remote_backend):
        remote_backend(_FakeRemoteEnv({"/root/raport_iulie_2026.xlsx": b"xlsx"}))

        safe, dropped = BasePlatformAdapter.filter_media_delivery_paths_with_drops(
            [("/root/raport_iulie_2026.xlsx", False)], "session-1"
        )

        assert dropped == []
        assert len(safe) == 1
        path, is_voice = safe[0]
        assert is_voice is False
        assert open(path, "rb").read() == b"xlsx"

    def test_failed_fetch_still_reports_the_drop(self, remote_backend):
        remote_backend(_FakeRemoteEnv({}))

        safe, dropped = BasePlatformAdapter.filter_media_delivery_paths_with_drops(
            [("/root/raport_iulie_2026.xlsx", False)], "session-1"
        )

        assert safe == []
        assert dropped == [("raport_iulie_2026.xlsx", MEDIA_DROP_MISSING)]

    def test_denied_local_path_is_not_laundered_through_the_backend(
        self, remote_backend, tmp_path, monkeypatch
    ):
        """A path rejected by the local denylist must not be re-fetched."""
        denied_root = tmp_path / "secrets"
        denied_root.mkdir()
        secret = denied_root / "id_rsa.pdf"
        secret.write_bytes(b"%PDF")
        monkeypatch.setattr(
            "gateway.platforms.base._MEDIA_DELIVERY_DENIED_PREFIXES",
            (str(denied_root),),
        )
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        env = remote_backend(_FakeRemoteEnv({str(secret): b"%PDF"}))

        safe, dropped = BasePlatformAdapter.filter_media_delivery_paths_with_drops(
            [(str(secret), False)], "session-1"
        )

        assert safe == []
        assert env.fetched == []
        assert dropped and dropped[0][1] == "denied"


class _SessionKeyedAdapter(BasePlatformAdapter):
    """Adapter whose session store maps session_key -> agent session_id."""

    def __init__(self, session_id):
        super().__init__(PlatformConfig(enabled=True, token="fake"), Platform.TELEGRAM)
        self.documents: list = []
        self.sent: list = []
        self._session_store = SimpleNamespace(peek_session_id=lambda key: session_id)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(content)
        return SendResult(success=True, message_id="m")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def send_document(self, chat_id, file_path, caption=None, **kwargs) -> SendResult:
        self.documents.append(str(file_path))
        return SendResult(success=True, message_id="doc")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


async def _hold_typing(_chat_id, interval=2.0, metadata=None, stop_event=None):
    if stop_event is not None:
        await stop_event.wait()
    else:
        await asyncio.Event().wait()


class TestSandboxLookupUsesTheAgentSessionId:
    """The sandbox is keyed by session_id, not by the gateway session_key.

    Regression: delivery looked the sandbox up under the session_key, missed a
    live container, and reported "no active docker terminal session" for a
    file that was sitting right there (observed in production 2026-08-18).
    """

    def test_adapter_maps_session_key_through_the_store(self):
        adapter = _SessionKeyedAdapter("20260815_234319_ef1efe4b")

        assert adapter.agent_task_id_for_session("telegram:123") == (
            "20260815_234319_ef1efe4b"
        )

    def test_adapter_falls_back_to_the_raw_key_without_a_store(self):
        adapter = _SessionKeyedAdapter("ignored")
        adapter._session_store = None

        assert adapter.agent_task_id_for_session("telegram:123") == "telegram:123"

    @pytest.mark.asyncio
    async def test_container_file_is_delivered_under_the_session_id(
        self, tmp_path, monkeypatch
    ):
        session_id = "20260815_234319_ef1efe4b"
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setattr(
            "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
        )
        env = _FakeRemoteEnv({"/root/incasari_dristor.csv": b"lucrator,total\n"})
        # The env is registered ONLY under the agent's session id — exactly the
        # production shape that made the session_key lookup fail.
        monkeypatch.setattr(
            "tools.terminal_tool.get_active_env",
            lambda task_id: env if task_id == session_id else None,
        )

        adapter = _SessionKeyedAdapter(session_id)
        adapter._keep_typing = _hold_typing

        async def handler(_event):
            return "Raportul e gata.\nMEDIA:/root/incasari_dristor.csv"

        adapter.set_message_handler(handler)
        event = MessageEvent(
            text="raport",
            source=SessionSource(
                platform=Platform.TELEGRAM, chat_id="111", chat_type="dm"
            ),
            message_id="m1",
        )

        await adapter._process_message_background(
            event, build_session_key(event.source)
        )

        assert len(adapter.documents) == 1, adapter.sent
        assert open(adapter.documents[0], "rb").read() == b"lucrator,total\n"
        assert all("Couldn't deliver" not in s for s in adapter.sent), adapter.sent
