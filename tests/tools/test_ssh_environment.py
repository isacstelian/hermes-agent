"""Tests for the SSH remote execution environment backend."""

import io
import json
import logging
import os
import shlex
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools.environments.ssh import SSHEnvironment
from tools.environments import ssh as ssh_env

_SSH_HOST = os.getenv("TERMINAL_SSH_HOST", "")
_SSH_USER = os.getenv("TERMINAL_SSH_USER", "")
_SSH_PORT = int(os.getenv("TERMINAL_SSH_PORT", "22"))
_SSH_KEY = os.getenv("TERMINAL_SSH_KEY", "")

_has_ssh = bool(_SSH_HOST and _SSH_USER)

requires_ssh = pytest.mark.skipif(
    not _has_ssh,
    reason="TERMINAL_SSH_HOST / TERMINAL_SSH_USER not set",
)


def _run(command, task_id="ssh_test", **kwargs):
    from tools.terminal_tool import terminal_tool
    return json.loads(terminal_tool(command, task_id=task_id, **kwargs))


def _cleanup(task_id="ssh_test"):
    from tools.terminal_tool import cleanup_vm
    cleanup_vm(task_id)


class TestBuildSSHCommand:

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]),
                                                      stderr=iter([]),
                                                      stdin=MagicMock()))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

    def test_base_flags(self, monkeypatch):
        # ControlMaster flags are POSIX-only (#73927): assert them only
        # where multiplexing is enabled so the test passes on Windows too.
        monkeypatch.setattr(ssh_env, "_SSH_MULTIPLEX", True)
        env = SSHEnvironment(host="h", user="u")
        cmd = " ".join(env._build_ssh_command())
        for flag in ("ControlMaster=auto", "ControlPersist=300",
                      "BatchMode=yes", "StrictHostKeyChecking=accept-new"):
            assert flag in cmd

    def test_controlmaster_gated_off_on_windows(self, monkeypatch):
        """#73927: Windows OpenSSH has no Unix-domain ControlMaster, so the
        ControlPath/ControlMaster/ControlPersist options must be omitted —
        passing them fails the connection with 'getsockname failed'."""
        monkeypatch.setattr(ssh_env, "_SSH_MULTIPLEX", False)
        env = SSHEnvironment(host="h", user="u")
        cmd = " ".join(env._build_ssh_command())
        assert "ControlMaster" not in cmd
        assert "ControlPath" not in cmd
        assert "ControlPersist" not in cmd
        # Non-multiplex flags must still be present — the backend works,
        # just without connection pooling.
        assert "BatchMode=yes" in cmd
        assert "StrictHostKeyChecking=accept-new" in cmd
        assert env._build_ssh_command()[-1] == "u@h"


    def test_user_host_suffix(self):
        env = SSHEnvironment(host="h", user="u")
        assert env._build_ssh_command()[-1] == "u@h"


class TestControlSocketPath:
    """Regression tests for issue #11840.

    macOS caps Unix domain socket paths at 104 bytes (sun_path). SSH
    appends a 16-byte random suffix to the control socket path when
    operating in ControlMaster mode. An IPv6 host embedded in the
    filename plus the deeply-nested macOS $TMPDIR easily blows past
    the limit, causing every tool call to fail immediately.
    """

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]),
                                                      stderr=iter([]),
                                                      stdin=MagicMock()))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

    # SSH appends ``.XXXXXXXXXXXXXXXX`` (17 bytes) to the ControlPath in
    # ControlMaster mode; the macOS sun_path field is 104 bytes including
    # the NUL terminator, so the usable path length is 103 bytes.
    _SSH_CONTROLMASTER_SUFFIX = 17
    _MAX_SUN_PATH = 103

    def test_fits_under_macos_socket_limit_with_ipv6_host(self, monkeypatch):
        """A realistic macOS $TMPDIR + IPv6 host must still produce a
        control socket path that fits once SSH appends its ControlMaster
        suffix (see issue #11840)."""
        # Simulate the macOS $TMPDIR shape from the issue traceback —
        # 48 bytes, the typical length of ``/var/folders/XX/YYYYYYYYY/T``.
        fake_tmp = "/var/folders/2t/wbkw5yb158jc3zhswgl7tz9c0000gn/T"
        monkeypatch.setattr("tools.environments.ssh.tempfile.gettempdir",
                            lambda: fake_tmp)
        # The simulated path doesn't exist on the test host — skip the
        # real mkdir so __init__ can proceed.
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "mkdir", lambda *a, **k: None)

        env = SSHEnvironment(
            host="9373:9b91:4480:558d:708e:e601:24e8:d8d0",
            user="hermes",
            port=22,
        )

        total_len = len(str(env.control_socket)) + self._SSH_CONTROLMASTER_SUFFIX
        assert total_len <= self._MAX_SUN_PATH, (
            f"control socket path would exceed the {self._MAX_SUN_PATH}-byte "
            f"Unix domain socket limit once SSH appends its 16-byte suffix: "
            f"{env.control_socket} (+{self._SSH_CONTROLMASTER_SUFFIX} = {total_len})"
        )

    def test_path_is_deterministic_across_instances(self):
        """Same (user, host, port) must yield the same control socket so
        ControlMaster reuse works across reconnects."""
        first = SSHEnvironment(host="example.com", user="alice", port=2222)
        second = SSHEnvironment(host="example.com", user="alice", port=2222)
        assert first.control_socket == second.control_socket

    def test_path_differs_for_different_targets(self):
        """Different (user, host, port) triples must produce different paths."""
        base = SSHEnvironment(host="h", user="u", port=22).control_socket
        assert SSHEnvironment(host="h", user="u", port=23).control_socket != base
        assert SSHEnvironment(host="h", user="v", port=22).control_socket != base
        assert SSHEnvironment(host="g", user="u", port=22).control_socket != base


class TestTerminalToolConfig:
    def test_ssh_persistent_default_true(self, monkeypatch):
        """SSH persistent defaults to True (via TERMINAL_PERSISTENT_SHELL)."""
        monkeypatch.delenv("TERMINAL_SSH_PERSISTENT", raising=False)
        monkeypatch.delenv("TERMINAL_PERSISTENT_SHELL", raising=False)
        from tools.terminal_tool import _get_env_config
        assert _get_env_config()["ssh_persistent"] is True


    def test_ssh_persistent_respects_config(self, monkeypatch):
        """TERMINAL_PERSISTENT_SHELL=false disables SSH persistent by default."""
        monkeypatch.delenv("TERMINAL_SSH_PERSISTENT", raising=False)
        monkeypatch.setenv("TERMINAL_PERSISTENT_SHELL", "false")
        from tools.terminal_tool import _get_env_config
        assert _get_env_config()["ssh_persistent"] is False


class TestSSHPreflight:
    def test_ensure_ssh_available_raises_clear_error_when_missing(self, monkeypatch):
        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: None)

        with pytest.raises(RuntimeError, match="SSH is not installed or not in PATH"):
            ssh_env._ensure_ssh_available()


    def test_ssh_environment_connects_when_ssh_exists(self, monkeypatch):
        called = {"count": 0}

        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")

        def _fake_establish(self):
            called["count"] += 1

        monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", _fake_establish)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/home/alice")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_ensure_remote_dirs", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)
        monkeypatch.setattr(ssh_env, "FileSyncManager", lambda **kw: type("M", (), {"sync": lambda self, **k: None})())

        env = ssh_env.SSHEnvironment(host="example.com", user="alice")

        assert called["count"] == 1
        assert env.host == "example.com"
        assert env.user == "alice"


class TestRemoteHermesBase:

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")
        monkeypatch.setattr(SSHEnvironment, "_establish_connection", lambda self: None)
        monkeypatch.setattr(SSHEnvironment, "_detect_remote_home", lambda self: "/home/testuser")
        monkeypatch.setattr(SSHEnvironment, "init_session", lambda self: None)
        monkeypatch.setattr(ssh_env, "FileSyncManager", MagicMock())
        monkeypatch.setattr(
            ssh_env.subprocess, "run",
            MagicMock(return_value=subprocess.CompletedProcess([], 0, stderr=b"")),
        )

    @pytest.mark.parametrize("value, expected_base", [
        (None, "/home/testuser/.hermes"),
        (".hermes-sandbox/junior-dev", "/home/testuser/.hermes-sandbox/junior-dev"),
        ("/srv/hermes-sandbox", "/srv/hermes-sandbox"),
        (".hermes-sandbox/./junior-dev/../junior-dev/", "/home/testuser/.hermes-sandbox/junior-dev"),
        ("/srv/./hermes-sandbox/../hermes-sandbox/", "/srv/hermes-sandbox"),
        ("/srv/hermes sandbox", "/srv/hermes sandbox"),
    ])
    def test_configured_base_used_for_all_sync_paths(self, monkeypatch, tmp_path,
                                                   value, expected_base):
        from hermes_cli import config

        if value is not None:
            (tmp_path / "config.yaml").write_text(
                json.dumps({"terminal": {"ssh_hermes_dir": value}}), encoding="utf-8",
            )
        load_config = MagicMock(wraps=config.load_config)
        monkeypatch.setattr(config, "load_config", load_config)

        env = SSHEnvironment(host="example.com", user="testuser")
        mkdir_cmd = ssh_env.subprocess.run.call_args.args[0][-1]
        assert shlex.split(mkdir_cmd) == [
            "mkdir", "-p", expected_base,
            f"{expected_base}/skills", f"{expected_base}/credentials", f"{expected_base}/cache",
        ]

        iter_files = MagicMock(return_value=[])
        monkeypatch.setattr(ssh_env, "iter_sync_files", iter_files)
        manager_kwargs = ssh_env.FileSyncManager.call_args.kwargs
        manager_kwargs["get_files_fn"]()
        iter_files.assert_called_once_with(expected_base)

        files = []
        for directory in ("skills", "credentials", "cache"):
            source = tmp_path / f"{directory}.txt"
            source.write_text(directory, encoding="utf-8")
            files.append((str(source), f"{expected_base}/{directory}/sample.txt"))

        def fake_popen(cmd, **kwargs):
            if cmd[0] == "tar":
                staging = Path(cmd[cmd.index("-C") + 1])
                for directory in ("skills", "credentials", "cache"):
                    assert (staging / directory / "sample.txt").read_text(encoding="utf-8") == directory
            proc = MagicMock()
            proc.returncode = 0
            proc.poll.return_value = 0
            proc.communicate.return_value = (b"", b"")
            proc.stderr.read.return_value = b""
            return proc

        popen = MagicMock(side_effect=fake_popen)
        monkeypatch.setattr(ssh_env.subprocess, "Popen", popen)
        manager_kwargs["bulk_upload_fn"](files)
        assert shlex.split(ssh_env.subprocess.run.call_args.args[0][-1]) == [
            "mkdir", "-p", f"{expected_base}/cache",
            f"{expected_base}/credentials", f"{expected_base}/skills",
        ]
        assert popen.call_args.args[0][-1] == (
            f"tar xf - --no-overwrite-dir -C {shlex.quote(expected_base)}"
        )

        manager_kwargs["bulk_download_fn"](tmp_path / "download.tar")
        assert ssh_env.subprocess.run.call_args.args[0][-1] == (
            f"tar cf - -C / {shlex.quote(expected_base.lstrip('/'))}"
        )
        assert env._remote_hermes_base == expected_base
        assert env.agent_visible_cache_base() == expected_base
        load_config.assert_called_once_with()

    @pytest.mark.parametrize("value", [
        "", "/", "//", "/srv/..", "../x", "..", "nested/../../x",
        "../testuser-other/x", "bad\npath", "bad\rpath", "bad\x00path", 123,
    ])
    def test_invalid_directory_falls_back_with_warning(self, tmp_path, caplog, value):
        (tmp_path / "config.yaml").write_text(
            json.dumps({"terminal": {"ssh_hermes_dir": value}}), encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger=ssh_env.__name__):
            env = SSHEnvironment(host="example.com", user="testuser")

        assert env._remote_hermes_base == "/home/testuser/.hermes"
        assert ssh_env.ssh_shell_hermes_base() == "~/.hermes"
        assert any(
            record.levelno == logging.WARNING
            and "terminal.ssh_hermes_dir" in record.message
            and ".hermes" in record.message
            for record in caplog.records
        )

    @pytest.mark.parametrize("empty_config", [None, {}, {"terminal": None}])
    def test_missing_config_falls_back(self, monkeypatch, empty_config):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: empty_config)
        env = SSHEnvironment(host="example.com", user="testuser")
        assert env._remote_hermes_base == "/home/testuser/.hermes"
        assert ssh_env.ssh_shell_hermes_base() == "~/.hermes"

    def test_config_loading_exception_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config", MagicMock(side_effect=OSError("cannot read config")),
        )
        env = SSHEnvironment(host="example.com", user="testuser")
        assert env._remote_hermes_base == "/home/testuser/.hermes"
        assert ssh_env.ssh_shell_hermes_base() == "~/.hermes"

    @pytest.mark.parametrize("config_text", ["", "terminal:\n  ssh_hermes_dir: null\n"])
    def test_default_preserves_remote_home_trailing_slash(self, monkeypatch, tmp_path, config_text):
        (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")
        monkeypatch.setattr(SSHEnvironment, "_detect_remote_home", lambda self: "/home/fixture/")

        env = SSHEnvironment(host="example.com", user="testuser")

        assert env._remote_hermes_base == "/home/fixture//.hermes"
        assert env.agent_visible_cache_base() == "/home/fixture//.hermes"

    def test_yaml_null_uses_default_without_warning(self, tmp_path, caplog):
        (tmp_path / "config.yaml").write_text(
            "terminal:\n  ssh_hermes_dir: null\n", encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger=ssh_env.__name__):
            env = SSHEnvironment(host="example.com", user="testuser")
            shell_base = ssh_env.ssh_shell_hermes_base()

        assert env._remote_hermes_base == "/home/testuser/.hermes"
        assert shell_base == "~/.hermes"
        assert not any(
            record.levelno >= logging.WARNING and record.name == ssh_env.__name__
            for record in caplog.records
        )

    @pytest.mark.parametrize("value, expected_base", [
        (None, "~/.hermes"),
        (".hermes-sandbox/junior-dev", "~/.hermes-sandbox/junior-dev"),
        (".hermes-sandbox/./junior-dev/../junior-dev/", "~/.hermes-sandbox/junior-dev"),
        ("/srv/x", "/srv/x"),
        ("/srv/./x/../x/", "/srv/x"),
    ])
    def test_shell_base_without_connection(self, monkeypatch, tmp_path, value, expected_base):
        if value is not None:
            (tmp_path / "config.yaml").write_text(
                json.dumps({"terminal": {"ssh_hermes_dir": value}}), encoding="utf-8",
            )
        connect = MagicMock(side_effect=AssertionError("must not connect"))
        monkeypatch.setattr(SSHEnvironment, "_establish_connection", connect)

        assert ssh_env.ssh_shell_hermes_base() == expected_base
        connect.assert_not_called()

    @pytest.mark.parametrize("value, expected_base", [
        (".hermes-sandbox/junior-dev", "/home/testuser/.hermes-sandbox/junior-dev"),
        ("/srv/hermes-sandbox", "/srv/hermes-sandbox"),
    ])
    def test_configured_base_syncs_back_with_real_manager(self, monkeypatch, tmp_path, request,
                                                        value, expected_base):
        from tools.environments.file_sync import FileSyncManager

        (tmp_path / "config.yaml").write_text(
            json.dumps({"terminal": {"ssh_hermes_dir": value}}), encoding="utf-8",
        )
        skill = tmp_path / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("original skill", encoding="utf-8")
        monkeypatch.setattr("tools.credential_files.get_credential_file_mounts", lambda: [])
        monkeypatch.setattr(ssh_env, "FileSyncManager", FileSyncManager)
        upload = MagicMock()
        monkeypatch.setattr(SSHEnvironment, "_ssh_bulk_upload", upload)

        env = SSHEnvironment(host="example.com", user="testuser")
        # Break the callback cycle before subprocess mocks are restored.
        request.addfinalizer(lambda: setattr(env, "_sync_manager", None))
        assert (str(skill), f"{expected_base}/skills/demo/SKILL.md") in upload.call_args.args[0]

        def download(cmd, **kwargs):
            assert cmd[-1] == f"tar cf - -C / {expected_base.lstrip('/')}"
            with tarfile.open(fileobj=kwargs["stdout"], mode="w") as archive:
                for name, data in {
                    "SKILL.md": b"edited skill",
                    "new.txt": b"new remote file",
                }.items():
                    info = tarfile.TarInfo(f"{expected_base.lstrip('/')}/skills/demo/{name}")
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            return subprocess.CompletedProcess([], 0, stderr=b"")

        monkeypatch.setattr(ssh_env.subprocess, "run", download)
        env.cleanup()

        assert skill.read_text(encoding="utf-8") == "edited skill"
        assert (skill.parent / "new.txt").read_text(encoding="utf-8") == "new remote file"


def _setup_ssh_env(monkeypatch, persistent: bool):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", _SSH_HOST)
    monkeypatch.setenv("TERMINAL_SSH_USER", _SSH_USER)
    monkeypatch.setenv("TERMINAL_SSH_PERSISTENT", "true" if persistent else "false")
    if _SSH_PORT != 22:
        monkeypatch.setenv("TERMINAL_SSH_PORT", str(_SSH_PORT))
    if _SSH_KEY:
        monkeypatch.setenv("TERMINAL_SSH_KEY", _SSH_KEY)


@requires_ssh
class TestOneShotSSH:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _setup_ssh_env(monkeypatch, persistent=False)
        yield
        _cleanup()

    def test_echo(self):
        r = _run("echo hello")
        assert r["exit_code"] == 0
        assert "hello" in r["output"]


    def test_state_does_not_persist(self):
        _run("export HERMES_ONESHOT_TEST=yes")
        r = _run("echo $HERMES_ONESHOT_TEST")
        assert r["output"].strip() == ""


@requires_ssh
class TestPersistentSSH:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _setup_ssh_env(monkeypatch, persistent=True)
        yield
        _cleanup()

    def test_echo(self):
        r = _run("echo hello-persistent")
        assert r["exit_code"] == 0
        assert "hello-persistent" in r["output"]

    def test_env_var_persists(self):
        _run("export HERMES_PERSIST_TEST=works")
        r = _run("echo $HERMES_PERSIST_TEST")
        assert r["output"].strip() == "works"


    def test_large_output(self):
        r = _run("seq 1 1000")
        assert r["exit_code"] == 0
        lines = r["output"].strip().splitlines()
        assert len(lines) == 1000
        assert lines[0] == "1"
        assert lines[-1] == "1000"
