"""Tests for fetching MEDIA files out of a terminal backend (#466, #75065).

Covers the remote-path credential denylist, the size cap, and the
transparent fetch through ``filter_media_delivery_paths_with_drops``: a
MEDIA path that exists only inside the agent's sandbox is pulled into the
document cache and delivered from there, while a denied path stays denied
and a failed fetch still produces the user-facing notice.
"""

import asyncio
import hashlib
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.media_fetch import (
    MEDIA_FETCH_MAX_BYTES_ENV,
    acquire_media_delivery_lease,
    fetch_remote_media,
    media_fetch_max_bytes,
    remote_path_is_denied,
    stage_inbound_media,
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

    def __init__(self, files=None, home="/root", links=None, remote_endpoint=True):
        self.files = files or {}
        self.links = links or {}
        self._remote_home = home
        self.remote_endpoint = remote_endpoint
        self.container_generation = 1
        self.fetched = []

    @property
    def remote_home(self):
        return self._remote_home

    def fetch_realpath(self, remote_path):
        return self.links.get(remote_path, remote_path)

    def fetch_file_size(self, remote_path):
        data = self.files.get(remote_path)
        return None if data is None else len(data)

    def fetch_file_metadata(self, remote_path):
        data = self.files.get(remote_path)
        if data is None:
            return None
        return len(data), hashlib.sha256(data).hexdigest()

    def fetch_file(self, remote_path, local_dest, max_bytes=None):
        data = self.files.get(remote_path)
        if data is None:
            raise FileFetchError(f"{remote_path!r} not found")
        if max_bytes is not None and len(data) > max_bytes:
            raise FileFetchError("artifact exceeds transfer limit")
        self.fetched.append(remote_path)
        with open(local_dest, "wb") as f:
            f.write(data)

    def put_file(self, local_source, remote_dest):
        with open(local_source, "rb") as stream:
            self.files[remote_dest] = stream.read()

    def execute(self, command, **_kwargs):
        argv = shlex.split(command)
        if argv[:3] == ["mkdir", "-p", "--"]:
            return {"returncode": 0, "output": ""}
        if argv[:3] == ["mv", "-f", "--"]:
            source, destination = argv[3:5]
            self.files[destination] = self.files.pop(source)
            return {"returncode": 0, "output": ""}
        if argv[:2] == ["ln", "--"]:
            source, destination = argv[2:4]
            self.files[destination] = self.files[source]
            return {"returncode": 0, "output": ""}
        if argv[:3] == ["rm", "-f", "--"]:
            self.files.pop(argv[3], None)
            return {"returncode": 0, "output": ""}
        return {"returncode": 1, "output": "unsupported command"}


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
        assert local and Path(local).read_bytes() == b"xlsx bytes"
        assert len(env.fetched) == 1
        assert env.fetched[0].startswith("/root/.raport.xlsx.hermes-artifact-")
        assert env.fetched[0].endswith(".snapshot")
        assert "raport.xlsx" in local

    def test_missing_in_backend_reports_reason(self, remote_backend):
        remote_backend(_FakeRemoteEnv({}))

        local, reason = fetch_remote_media("/root/gone.xlsx", "session-1")

        assert local is None
        assert "not in the agent sandbox" in reason

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

    def test_platform_limit_overrides_global_fetch_default(self, remote_backend):
        env = remote_backend(_FakeRemoteEnv({"/root/big.zip": b"x" * 2048}))

        local, reason = fetch_remote_media(
            "/root/big.zip", "session-1", max_bytes=1024
        )

        assert local is None
        assert "1.0 KB delivery limit" in reason
        assert env.fetched == []

    def test_no_active_session_reports_reason(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setattr("tools.terminal_tool.get_active_env", lambda task_id: None)

        local, reason = fetch_remote_media("/root/raport.xlsx", "session-1")

        assert local is None
        assert "no active docker terminal session" in reason

    def test_session_miss_does_not_fall_back_to_foreign_default_env(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setattr(
            "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
        )
        foreign = _FakeRemoteEnv({"/root/raport.xlsx": b"foreign session"})
        monkeypatch.setattr(
            "tools.terminal_tool.get_active_env",
            lambda task_id: foreign if task_id == "default" else None,
        )

        local, reason = fetch_remote_media("/root/raport.xlsx", "session-1")

        assert local is None
        assert "no active docker terminal session" in reason
        assert foreign.fetched == []

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


class TestMediaDeliveryLease:
    def test_remote_backend_acquires_before_environment_exists(self, monkeypatch):
        sentinel = object()
        seen = []
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setattr(
            "tools.terminal_tool.acquire_environment_lease",
            lambda task_id: seen.append(task_id) or sentinel,
        )

        assert acquire_media_delivery_lease("session-1") is sentinel
        assert seen == ["session-1"]

    def test_local_backend_does_not_acquire(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        resolved = []

        assert acquire_media_delivery_lease(
            task_id_factory=lambda: resolved.append(True) or "session-1"
        ) is None
        assert resolved == []


class TestInboundMediaStaging:
    @pytest.fixture()
    def cache_mount(self, tmp_path, monkeypatch):
        host_cache = tmp_path / "documents"
        host_cache.mkdir()
        container_cache = "/root/.hermes/cache/documents"
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setattr(
            "tools.credential_files.get_cache_directory_mounts",
            lambda **_kwargs: [{
                "host_path": str(host_cache),
                "container_path": container_cache,
            }],
        )
        return host_cache, container_cache

    @pytest.mark.parametrize("agent_visible", [False, True])
    def test_pushes_direct_and_replied_document_paths(
        self, cache_mount, monkeypatch, agent_visible
    ):
        host_cache, container_cache = cache_mount
        document = host_cache / "Audit Creste cu Magic.pdf"
        document.write_bytes(b"%PDF-1.7\ntelegram attachment")
        env = _FakeRemoteEnv()
        monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda task_id: env)
        input_path = (
            f"{container_cache}/{document.name}" if agent_visible else str(document)
        )

        failures = stage_inbound_media([input_path], "session-1")

        assert failures == []
        assert env.files[f"{container_cache}/{document.name}"] == document.read_bytes()

    def test_local_daemon_relies_on_bind_mount(self, cache_mount, monkeypatch):
        host_cache, _container_cache = cache_mount
        document = host_cache / "report.pdf"
        document.write_bytes(b"%PDF")
        env = _FakeRemoteEnv(remote_endpoint=False)
        monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda task_id: env)

        assert stage_inbound_media([str(document)], "session-1") == []
        assert env.files == {}

    def test_missing_remote_environment_is_reported(self, cache_mount, monkeypatch):
        host_cache, _container_cache = cache_mount
        document = host_cache / "report.pdf"
        document.write_bytes(b"%PDF")
        monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda task_id: None)

        failures = stage_inbound_media([str(document)], "session-1")

        assert failures == [("report.pdf", "remote Docker environment unavailable")]

    @pytest.mark.asyncio
    async def test_gateway_stages_event_media_before_agent_run(self, monkeypatch):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.config = SimpleNamespace(multiplex_profiles=False)
        runner._prepare_inbound_message_text = AsyncMock(return_value="read attachment")
        runner._agent_task_id_for_source = lambda source: "agent-session"
        staged = []
        monkeypatch.setattr(
            "gateway.media_fetch.stage_inbound_media",
            lambda paths, task_id: staged.append((paths, task_id)) or [],
        )
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="123")
        event = MessageEvent(
            text="read it",
            source=source,
            media_urls=["/root/.hermes/cache/documents/Audit.pdf"],
        )

        prepared = await runner._prepare_profile_scoped_inbound_message_text(
            event=event, source=source, history=[]
        )

        assert prepared == "read attachment"
        assert staged == [([event.media_urls[0]], "agent-session")]



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
        assert Path(path).read_bytes() == b"xlsx"

    @pytest.mark.parametrize("name", ["Caddyfile", "script.py", "data.weirdext"])
    def test_explicit_media_is_extension_agnostic(self, remote_backend, name):
        remote_path = f"/workspace/{name}"
        remote_backend(_FakeRemoteEnv({remote_path: b"artifact"}))

        media, cleaned = BasePlatformAdapter.extract_media(f"done\nMEDIA:{remote_path}")
        safe, dropped = BasePlatformAdapter.filter_media_delivery_paths_with_drops(
            media, "session-1"
        )

        assert cleaned.strip() == "done"
        assert dropped == []
        assert len(safe) == 1
        assert Path(safe[0][0]).read_bytes() == b"artifact"

    def test_failed_fetch_still_reports_the_drop(self, remote_backend):
        remote_backend(_FakeRemoteEnv({}))

        safe, dropped = BasePlatformAdapter.filter_media_delivery_paths_with_drops(
            [("/root/raport_iulie_2026.xlsx", False)], "session-1"
        )

        assert safe == []
        name, reason, detail = dropped[0]
        assert (name, reason) == ("raport_iulie_2026.xlsx", MEDIA_DROP_MISSING)
        # The drop carries the backend's own explanation, so the user is told
        # the file was never in the sandbox rather than a generic path error.
        assert "not in the agent sandbox" in detail

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
        assert Path(adapter.documents[0]).read_bytes() == b"lucrator,total\n"
        assert all("Couldn't deliver" not in s for s in adapter.sent), adapter.sent

    @pytest.mark.asyncio
    async def test_session_rotation_keeps_pre_rotation_artifact_environment(
        self, tmp_path, monkeypatch
    ):
        old_session = "session-before-compression"
        current_session = [old_session]
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setattr(
            "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
        )
        env = _FakeRemoteEnv({"/root/report.pdf": b"%PDF-old-session"})
        monkeypatch.setattr(
            "tools.terminal_tool.get_active_env",
            lambda task_id: env if task_id == old_session else None,
        )
        adapter = _SessionKeyedAdapter(old_session)
        adapter._session_store = SimpleNamespace(
            peek_session_id=lambda _key: current_session[0]
        )
        adapter._keep_typing = _hold_typing

        async def handler(_event):
            current_session[0] = "session-after-compression"
            return "MEDIA:/root/report.pdf"

        adapter.set_message_handler(handler)
        event = MessageEvent(
            text="raport",
            source=SessionSource(platform=Platform.TELEGRAM, chat_id="111"),
        )

        await adapter._process_message_background(
            event, build_session_key(event.source)
        )

        assert len(adapter.documents) == 1, adapter.sent
        assert Path(adapter.documents[0]).read_bytes() == b"%PDF-old-session"

    @pytest.mark.asyncio
    async def test_adapter_holds_environment_lease_through_delivery(self, monkeypatch):
        session_id = "session-with-artifact"
        acquired = []
        released = []
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setattr(
            "tools.terminal_tool.acquire_environment_lease",
            lambda task_id: acquired.append(task_id)
            or SimpleNamespace(release=lambda: released.append(task_id)),
        )
        adapter = _SessionKeyedAdapter(session_id)
        adapter._keep_typing = _hold_typing
        adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="done"))
        event = MessageEvent(
            text="run",
            source=SessionSource(
                platform=Platform.TELEGRAM, chat_id="111", chat_type="dm"
            ),
        )

        await adapter._process_message_background(
            event, build_session_key(event.source)
        )

        assert acquired == [session_id]
        assert released == [session_id]
