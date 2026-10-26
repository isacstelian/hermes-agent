"""Tests for docker container_config key propagation in file_tools."""

from unittest.mock import patch, MagicMock
import subprocess

import pytest
import tools.file_tools as file_tools
from tools import terminal_tool


def _make_env_config(**overrides):
    base = {
        "env_type": "docker",
        "docker_image": "test-image:latest",
        "singularity_image": "docker://test",
        "modal_image": "test",
        "daytona_image": "test",
        "cwd": "/workspace",
        "host_cwd": None,
        "timeout": 180,
        "container_cpu": 2,
        "container_memory": 4096,
        "container_disk": 20480,
        "container_persistent": False,
        "docker_volumes": [],
        "docker_mount_cwd_to_workspace": True,
        "docker_forward_env": ["MY_SECRET", "API_KEY"],
    }
    base.update(overrides)
    return base


class TestFileToolsContainerConfig:
    def _run(self, env_config, task_id, task_env_overrides=None):
        captured = {}
        mock_env = MagicMock()

        def fake_create_env(**kwargs):
            captured.update(kwargs)
            return mock_env

        with patch("tools.terminal_tool._get_env_config", return_value=env_config), \
             patch("tools.terminal_tool._task_env_overrides", task_env_overrides or {}), \
             patch("tools.terminal_tool._active_environments", {}), \
             patch("tools.terminal_tool._creation_locks", {}), \
             patch("tools.terminal_tool._creation_locks_lock", __import__("threading").Lock()), \
             patch("tools.terminal_tool._create_environment", side_effect=fake_create_env), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._check_disk_usage_warning"), \
             patch("tools.file_tools._file_ops_cache", {}), \
             patch("tools.file_tools._file_ops_lock", __import__("threading").Lock()):
            file_tools._get_file_ops(task_id)

        return captured

    def test_docker_mount_cwd_to_workspace_passed(self):
        """docker_mount_cwd_to_workspace is forwarded to container_config."""
        cc = self._run(_make_env_config(docker_mount_cwd_to_workspace=True), "t1").get("container_config", {})
        assert cc.get("docker_mount_cwd_to_workspace") is True

    def test_container_config_matches_terminal(self):
        config = _make_env_config(
            docker_extra_args=["--runtime=runsc", "--read-only", "--user=1000:1000"],
            docker_env={"APP_MODE": "test"}, docker_shm_size="2g",
            docker_network=False,
            docker_persist_across_processes=False, docker_orphan_reaper=False,
        )
        captured = self._run(config, "file-first")
        assert captured["container_config"] == terminal_tool._container_config_from_config(config)


    def test_cwd_only_raw_task_override_reaches_file_environment(self):
        """CWD-only task overrides collapse to default but must keep their cwd."""
        captured = self._run(
            _make_env_config(env_type="local", cwd="/config-cwd"),
            "desktop-session-cwd",
            task_env_overrides={"desktop-session-cwd": {"cwd": "/workspace/session"}},
        )

        assert captured["task_id"] == "default"
        assert captured["cwd"] == "/workspace/session"


@pytest.mark.parametrize("creator", ["file", "code"])
def test_lazy_container_is_reused_by_terminal_without_stale_removal(monkeypatch, tmp_path, creator):
    from tools import code_execution_tool
    from tools.environments import docker as docker_env

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    extra_args = ["--runtime=runsc", "--read-only", "--user=1000:1000"]
    config = _make_env_config(
        container_persistent=True, host_cwd=str(workspace),
        docker_extra_args=extra_args, docker_env={"APP_MODE": "test"},
        docker_forward_env=[], docker_orphan_reaper=False,
    )
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    for name in ("_active_environments", "_last_activity", "_creation_locks", "_task_env_overrides"):
        monkeypatch.setattr(terminal_tool, name, {})
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_check_disk_usage_warning", lambda: None)
    monkeypatch.setattr(terminal_tool, "_maybe_reap_docker_orphans", lambda *_: None)
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env, "_cgroup_limits_ok", True)
    monkeypatch.setattr(docker_env.DockerEnvironment, "init_session", lambda self: None)
    containers = {}
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        output = ""
        if cmd[1] == "version":
            output = "Docker version"
        elif cmd[1] == "run":
            labels = dict(cmd[i + 1].split("=", 1) for i, arg in enumerate(cmd) if arg == "--label")
            cid = f"container-{sum(call[1] == 'run' for call in calls)}"
            containers[cid] = labels
            output = cid + "\n"
        elif cmd[1] == "rm":
            containers.pop(cmd[-1], None)
        elif cmd[1] == "ps":
            filters = [cmd[i + 1][6:].split("=", 1) for i, arg in enumerate(cmd)
                       if arg == "--filter" and cmd[i + 1].startswith("label=")]
            fmt = cmd[cmd.index("--format") + 1]
            rows = []
            for cid, labels in containers.items():
                if not all(labels.get(key) == value for key, value in filters):
                    continue
                if ".State" in fmt:
                    rows.append(f"{cid}\trunning\t{labels['hermes-egress']}")
                else:
                    rows.append("\t".join([cid, labels["hermes-mounts"], labels["hermes-egress"],
                                           labels["hermes-task-key"], labels["hermes-profile-key"]]))
            output = "\n".join(rows)
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", run)
    monkeypatch.setattr(docker_env, "_popen_bash", lambda *_: pytest.fail("unexpected Docker exec"))
    if creator == "file":
        file_tools._get_file_ops("sa-1-abcd1234")
    else:
        code_execution_tool._get_or_create_env("sa-1-abcd1234")
    lazy_env = terminal_tool.get_active_env("default")
    # Simulate a new handle attaching to the same daemon-side container.
    terminal_tool._active_environments.clear()
    terminal_env = terminal_tool.ensure_task_env("terminal-session")

    assert terminal_env is not None
    assert terminal_env._container_id == lazy_env._container_id
    assert terminal_env._labels == lazy_env._labels
    assert terminal_env._all_run_args == lazy_env._all_run_args
    run_commands = [cmd for cmd in calls if cmd[1] == "run"]
    assert len(run_commands) == 1
    assert all(arg in run_commands[0] for arg in extra_args)
    assert not any(cmd[1] == "rm" for cmd in calls)
