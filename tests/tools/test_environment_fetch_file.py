"""Tests for the backend file-extraction API (issues #466, #75065).

Covers ``BaseEnvironment.fetch_file`` / ``fetch_file_size`` (the
base64-over-exec default that works on any backend with a shell) and the
Docker override, which reads the bind-mount host view when it exists and
otherwise streams through ``docker cp`` — the path that matters when the
Docker daemon is remote and no host-side view exists at all.
"""

import base64
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.environments.base import BaseEnvironment, FileFetchError


class _FakeExecEnvironment(BaseEnvironment):
    """BaseEnvironment with execute() stubbed to canned results."""

    def __init__(self, results):
        # Skip BaseEnvironment.__init__ side effects — set only what
        # fetch_file / fetch_file_size actually touch.
        self.timeout = 60
        self._results = list(results)
        self.commands = []

    def execute(self, command, cwd="", **kwargs):
        self.commands.append(command)
        return self._results.pop(0)

    def cleanup(self):
        pass


class TestBaseFetchFile:
    def test_decodes_marker_fenced_payload(self, tmp_path):
        payload = b"%PDF-1.4 binary\x00\x01 content"
        encoded = base64.b64encode(payload).decode()

        def fake_execute(command, cwd="", **kwargs):
            marker = command.split("echo ")[1].split(" &&")[0]
            return {"output": f"{marker}\n{encoded}\n{marker}\n", "returncode": 0}

        env = _FakeExecEnvironment([])
        env.execute = fake_execute
        dest = tmp_path / "out.pdf"

        env.fetch_file("/workspace/report.pdf", str(dest))

        assert dest.read_bytes() == payload

    def test_ignores_noise_outside_the_markers(self, tmp_path):
        """A login shell's motd must not corrupt the decode."""
        payload = b"hello world"
        encoded = base64.b64encode(payload).decode()

        def fake_execute(command, cwd="", **kwargs):
            marker = command.split("echo ")[1].split(" &&")[0]
            return {
                "output": f"motd: welcome!\n{marker}\n{encoded}\n{marker}\ntrailing\n",
                "returncode": 0,
            }

        env = _FakeExecEnvironment([])
        env.execute = fake_execute
        dest = tmp_path / "out.txt"

        env.fetch_file("/tmp/hello.txt", str(dest))

        assert dest.read_bytes() == payload

    def test_missing_file_raises(self, tmp_path):
        env = _FakeExecEnvironment([{"output": "", "returncode": 1}])

        with pytest.raises(FileFetchError, match="could not read"):
            env.fetch_file("/nope.txt", str(tmp_path / "out"))

    def test_corrupt_payload_raises(self, tmp_path):
        def fake_execute(command, cwd="", **kwargs):
            marker = command.split("echo ")[1].split(" &&")[0]
            return {"output": f"{marker}\nnot!!valid@@b64\n{marker}\n", "returncode": 0}

        env = _FakeExecEnvironment([])
        env.execute = fake_execute

        with pytest.raises(FileFetchError, match="corrupted"):
            env.fetch_file("/tmp/x.bin", str(tmp_path / "out"))

    def test_size_parses_last_digit_token(self):
        env = _FakeExecEnvironment([{"output": "banner\n  1234\n", "returncode": 0}])

        assert env.fetch_file_size("/tmp/x.bin") == 1234

    def test_size_of_missing_file_is_none(self):
        env = _FakeExecEnvironment([{"output": "", "returncode": 1}])

        assert env.fetch_file_size("/nope") is None

    def test_remote_home_defaults_to_none(self):
        assert _FakeExecEnvironment([]).remote_home is None


class TestDockerFetchFile:
    def _make_env(self, home_dir=None, workspace_dir=None):
        from tools.environments.docker import DockerEnvironment

        env = DockerEnvironment.__new__(DockerEnvironment)
        env._home_dir = home_dir
        env._workspace_dir = workspace_dir
        env._container_id = "cafebabe1234"
        env._docker_exe = "docker"
        return env

    def test_bind_mounted_root_path_copies_from_host(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        (home / "report.pdf").write_bytes(b"%PDF data")
        env = self._make_env(home_dir=str(home))
        dest = tmp_path / "out.pdf"

        with patch("tools.environments.docker.subprocess.run") as run_mock:
            env.fetch_file("/root/report.pdf", str(dest))

        run_mock.assert_not_called()
        assert dest.read_bytes() == b"%PDF data"

    def test_traversal_out_of_the_mount_does_not_map(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        env = self._make_env(home_dir=str(home))

        # /root/../etc/passwd normalizes to /etc/passwd: no host mapping, so
        # it falls through to docker cp (and the caller's denylist).
        assert env._host_path_for("/root/../etc/passwd") is None

    def test_remote_daemon_path_uses_docker_cp(self, tmp_path):
        """No host-side view (tmpfs, or a daemon on another machine)."""
        env = self._make_env()
        dest = tmp_path / "out.xlsx"

        def fake_run(cmd, capture_output=None, text=None, timeout=None, stdin=None):
            assert cmd[:3] == ["docker", "cp", "-L"]
            assert cmd[3] == "cafebabe1234:/root/raport.xlsx"
            Path(cmd[4]).write_bytes(b"copied")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("tools.environments.docker.subprocess.run", side_effect=fake_run):
            env.fetch_file("/root/raport.xlsx", str(dest))

        assert dest.read_bytes() == b"copied"

    def test_docker_cp_failure_raises_with_stderr(self, tmp_path):
        env = self._make_env()

        def fake_run(cmd, capture_output=None, text=None, timeout=None, stdin=None):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such file")

        with patch("tools.environments.docker.subprocess.run", side_effect=fake_run):
            with pytest.raises(FileFetchError, match="no such file"):
                env.fetch_file("/nope.txt", str(tmp_path / "out"))

    def test_directory_result_is_rejected(self, tmp_path):
        env = self._make_env()
        dest = tmp_path / "out"

        def fake_run(cmd, capture_output=None, text=None, timeout=None, stdin=None):
            Path(cmd[4]).mkdir()
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("tools.environments.docker.subprocess.run", side_effect=fake_run):
            with pytest.raises(FileFetchError, match="not a regular file"):
                env.fetch_file("/some/dir", str(dest))

        assert not dest.exists()

    def test_no_container_raises(self, tmp_path):
        env = self._make_env()
        env._container_id = None

        with pytest.raises(FileFetchError, match="not started"):
            env.fetch_file("/root/x.txt", str(tmp_path / "out"))
