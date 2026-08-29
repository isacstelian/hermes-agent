"""Tests for the backend file-extraction API (issues #466, #75065).

Covers ``BaseEnvironment.fetch_file`` / ``fetch_file_size`` (the
base64-over-exec default that works on any backend with a shell) and the
Docker override, which reads the bind-mount host view when it exists and
otherwise streams through ``docker cp`` — the path that matters when the
Docker daemon is remote and no host-side view exists at all.
"""

import base64
import hashlib
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_docker_cp_miss_falls_back_to_raw_tar_before_base64(self, tmp_path):
        """tmpfs + gVisor: the archive API cannot see /root, exec can.

        Production shape (2026-08-18): the agent wrote a valid .xlsx to
        /root inside a runsc container whose /root is a tmpfs. ``docker cp``
        answered "Could not find the file" because the archive API reads the
        rootfs layers, not the mounts stacked on them, while ``docker exec``
        read the same file fine.
        """
        env = self._make_env()
        dest = tmp_path / "raport.xlsx"

        def fake_run(cmd, capture_output=None, text=None, timeout=None, stdin=None):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="",
                stderr="Error response from daemon: Could not find the file "
                       "/root/raport.xlsx in container cafebabe1234",
            )

        env._fetch_file_with_tar = Mock(
            side_effect=lambda _source, target: Path(target).write_bytes(b"tar-stream")
        )
        env.execute = Mock(side_effect=AssertionError("base64 fallback must not run"))
        with patch("tools.environments.docker.subprocess.run", side_effect=fake_run):
            env.fetch_file("/root/raport.xlsx", str(dest))

        env._fetch_file_with_tar.assert_called_once_with(
            "/root/raport.xlsx", str(dest)
        )
        assert dest.read_bytes() == b"tar-stream"

    def test_bounded_pull_uses_archive_api_before_exec_tar(self, tmp_path):
        env = self._make_env()
        payload = b"x" * (9 * 1024 * 1024)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as stream:
            info = tarfile.TarInfo("report.pdf")
            info.size = len(payload)
            stream.addfile(info, io.BytesIO(payload))

        commands = []

        class FakeProcess:
            def __init__(self, command, **_kwargs):
                commands.append(command)
                self.stdout = io.BytesIO(archive.getvalue())
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def kill(self):
                self.returncode = -9

        env._fetch_file_with_tar = Mock(
            side_effect=AssertionError("exec-tar fallback must not run")
        )
        env.execute = Mock(side_effect=AssertionError("base64 fallback must not run"))
        destination = tmp_path / "report.pdf"

        with patch("tools.environments.docker.subprocess.Popen", FakeProcess):
            env.fetch_file(
                "/workspace/report.pdf",
                str(destination),
                max_bytes=10 * 1024 * 1024,
            )

        assert commands == [
            [
                "docker",
                "cp",
                "-L",
                "cafebabe1234:/workspace/report.pdf",
                "-",
            ]
        ]
        assert destination.read_bytes() == payload

    def test_docker_cp_miss_with_no_exec_read_still_raises(self, tmp_path):
        env = self._make_env()

        def fake_run(cmd, capture_output=None, text=None, timeout=None, stdin=None):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such file")

        env._fetch_file_with_tar = Mock(
            side_effect=FileFetchError("tar stream failed")
        )
        env.execute = lambda command, cwd="", **kwargs: {"output": "", "returncode": 1}
        with patch("tools.environments.docker.subprocess.run", side_effect=fake_run):
            with pytest.raises(FileFetchError, match="tar stream failed"):
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

    def test_raw_tar_pull_preserves_arbitrary_bytes(self, tmp_path):
        env = self._make_env()
        payload = b"\x00\xffPK\x03\x04binary"
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as stream:
            info = tarfile.TarInfo("blob")
            info.size = len(payload)
            stream.addfile(info, io.BytesIO(payload))

        def fake_run(cmd, **kwargs):
            assert cmd[:3] == ["docker", "exec", "cafebabe1234"]
            assert cmd[3:6] == ["tar", "-cf", "-"]
            kwargs["stdout"].write(archive.getvalue())
            return subprocess.CompletedProcess(cmd, 0, stdout=None, stderr=b"")

        destination = tmp_path / "blob"
        with patch("tools.environments.docker.subprocess.run", side_effect=fake_run):
            env._fetch_file_with_tar("/workspace/blob", str(destination))

        assert destination.read_bytes() == payload

    def test_raw_tar_pull_rejects_member_above_transport_limit(self, tmp_path):
        env = self._make_env()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as stream:
            info = tarfile.TarInfo("blob")
            info.size = 5
            stream.addfile(info, io.BytesIO(b"12345"))

        class FakeProcess:
            def __init__(self, *_args, **_kwargs):
                self.stdout = io.BytesIO(archive.getvalue())
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def kill(self):
                self.returncode = -9

        destination = tmp_path / "blob"
        with patch("tools.environments.docker.subprocess.Popen", FakeProcess):
            with pytest.raises(FileFetchError, match="transfer limit"):
                env._fetch_file_with_tar(
                    "/workspace/blob", str(destination), max_bytes=4
                )

        assert not destination.exists()

    def test_docker_put_uses_cp_fast_path(self, tmp_path):
        env = self._make_env()
        source = tmp_path / "blob"
        source.write_bytes(b"payload")
        env.fetch_file_metadata = Mock(
            return_value=(7, "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5")
        )

        with patch(
            "tools.environments.docker.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ) as run_mock:
            env.put_file(str(source), "/workspace/blob")

        assert run_mock.call_args.args[0] == [
            "docker", "cp", "-L", str(source), "cafebabe1234:/workspace/blob"
        ]

    def test_docker_put_cp_success_but_missing_target_falls_back_to_tar(
        self, tmp_path
    ):
        env = self._make_env()
        source = tmp_path / "blob"
        source.write_bytes(b"payload")
        env.fetch_file_metadata = Mock(return_value=None)
        env._put_file_with_tar = Mock()

        with patch(
            "tools.environments.docker.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ):
            env.put_file(str(source), "/workspace/blob")

        env._put_file_with_tar.assert_called_once_with(
            str(source), "/workspace/blob"
        )

    def test_docker_put_cp_failure_falls_back_to_raw_tar(self, tmp_path):
        env = self._make_env()
        source = tmp_path / "blob"
        source.write_bytes(b"payload")
        env._put_file_with_tar = Mock()
        env.execute = Mock(side_effect=AssertionError("base64 fallback must not run"))

        with patch(
            "tools.environments.docker.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="cp failed"),
        ):
            env.put_file(str(source), "/workspace/blob")

        env._put_file_with_tar.assert_called_once_with(
            str(source), "/workspace/blob"
        )

    def test_raw_tar_push_preserves_arbitrary_bytes(self, tmp_path):
        env = self._make_env()
        payload = b"\x00\xffarbitrary bytes"
        source = tmp_path / "blob"
        source.write_bytes(payload)

        def fake_run(cmd, **kwargs):
            assert cmd[:4] == ["docker", "exec", "-i", "cafebabe1234"]
            assert cmd[4:7] == ["tar", "-xf", "-"]
            with tarfile.open(fileobj=kwargs["stdin"], mode="r:") as stream:
                member = stream.next()
                assert member is not None
                extracted = stream.extractfile(member)
                assert extracted is not None
                assert extracted.read() == payload
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with patch("tools.environments.docker.subprocess.run", side_effect=fake_run):
            env._put_file_with_tar(str(source), "/workspace/blob")

    def test_docker_metadata_batch_uses_one_exec(self):
        env = self._make_env()
        payloads = {
            "/root/.hermes/skills/a": b"one",
            "/root/.hermes/skills/$(touch nope)": b"two",
        }
        calls = []

        def fake_execute(command, **kwargs):
            calls.append((command, kwargs))
            marker = re.search(r"(__HERMES_META_[0-9a-f]+__)", command).group(1)
            lines = []
            for index, path in enumerate(kwargs["stdin_data"].splitlines()):
                digest = hashlib.sha256(payloads[path]).hexdigest()
                lines.append(f"{marker}{index} {len(payloads[path])} {digest}")
            return {"returncode": 0, "output": "\n".join(lines)}

        env.execute = fake_execute
        result = env.fetch_file_metadata_many(payloads)

        assert len(calls) == 1
        assert result == {
            path: (len(payload), hashlib.sha256(payload).hexdigest())
            for path, payload in payloads.items()
        }
        assert "eval" not in calls[0][0]

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="simulates commands executed inside a Linux Docker container",
    )
    def test_docker_metadata_batch_hashes_532_real_files(self, tmp_path):
        env = self._make_env()
        payloads = {}
        for index in range(532):
            name = f"file {index:03d} $(not-executed).bin"
            path = tmp_path / name
            payload = f"payload-{index}".encode()
            path.write_bytes(payload)
            payloads[str(path)] = payload
        missing = str(tmp_path / "missing file")
        calls = []

        def shell_execute(command, **kwargs):
            calls.append(command)
            result = subprocess.run(
                ["bash", "-c", command],
                input=kwargs["stdin_data"],
                capture_output=True,
                text=True,
                check=False,
            )
            return {"returncode": result.returncode, "output": result.stdout + result.stderr}

        env.execute = shell_execute
        result = env.fetch_file_metadata_many([*payloads, missing])

        assert len(calls) == 1
        assert result == {
            **{
                path: (len(payload), hashlib.sha256(payload).hexdigest())
                for path, payload in payloads.items()
            },
            missing: None,
        }
        assert 'sha256sum "$path"' not in calls[0]

    def test_docker_artifact_session_disables_transparent_recreation(self):
        from tools.environments.docker import DockerEnvironment

        env = DockerEnvironment.__new__(DockerEnvironment)
        env._container_generation = 3
        env._persist_across_processes = True
        env._recreate_container = Mock(return_value=True)

        with patch.object(
            BaseEnvironment,
            "execute",
            return_value={"returncode": 1, "output": "No such container"},
        ):
            with env.artifact_session(3):
                result = env.execute("true")

        assert result["returncode"] == 1
        env._recreate_container.assert_not_called()

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="renameat2 directory exchange is a Linux container primitive",
    )
    def test_docker_directory_publication_uses_atomic_exchange(self, tmp_path):
        env = self._make_env()
        source = tmp_path / "staging"
        destination = tmp_path / "live"
        source.mkdir()
        destination.mkdir()
        (source / "new.txt").write_text("new")
        (destination / "old.txt").write_text("old")

        def shell_execute(command, **_kwargs):
            result = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                check=False,
            )
            return {"returncode": result.returncode, "output": result.stdout + result.stderr}

        env.execute = shell_execute

        assert env.publish_directory_atomic(str(source), str(destination)) is True
        assert (destination / "new.txt").read_text() == "new"
        assert (source / "old.txt").read_text() == "old"
        assert env.publish_directory_atomic(str(destination), str(source)) is True
        assert (destination / "old.txt").read_text() == "old"
        assert (source / "new.txt").read_text() == "new"

        first_publish = tmp_path / "first-publish"
        assert env.publish_directory_atomic(str(source), str(first_publish)) is False
        assert (first_publish / "new.txt").read_text() == "new"
        assert not source.exists()

    def test_docker_archive_push_streams_one_tar(self, tmp_path):
        env = self._make_env()
        archive_path = tmp_path / "skills.tar"
        with tarfile.open(archive_path, "w") as archive:
            member = tarfile.TarInfo("report/SKILL.md")
            member.size = 8
            archive.addfile(member, io.BytesIO(b"# Report"))

        def fake_run(command, **kwargs):
            assert command == [
                "docker", "exec", "-i", "cafebabe1234",
                "tar", "-xf", "-", "-C", "/root/.hermes/skills-stage",
            ]
            with tarfile.open(fileobj=kwargs["stdin"], mode="r:") as archive:
                assert archive.getnames() == ["report/SKILL.md"]
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

        with patch("tools.environments.docker.subprocess.run", side_effect=fake_run):
            env.put_archive(str(archive_path), "/root/.hermes/skills-stage")

    def test_docker_archive_push_rejects_traversal_member(self, tmp_path):
        env = self._make_env()
        archive_path = tmp_path / "unsafe.tar"
        with tarfile.open(archive_path, "w") as archive:
            member = tarfile.TarInfo("../../etc/passwd")
            member.size = 3
            archive.addfile(member, io.BytesIO(b"bad"))

        with patch("tools.environments.docker.subprocess.run") as run_mock:
            with pytest.raises(FileFetchError, match="unsafe member"):
                env.put_archive(str(archive_path), "/root/.hermes/skills-stage")

        run_mock.assert_not_called()
