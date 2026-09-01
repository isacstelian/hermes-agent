"""Regression tests for the Docker terminal network toggle.

Ported from NanoClaw PR #2713's opt-in egress lockdown idea. Hermes already
has DockerEnvironment(network=False), but the terminal config path did not
expose it, so operators could not request networkless Docker execution from
config.yaml.
"""

import pytest

import tools.terminal_tool as terminal_tool
from tools.environments import docker as docker_env


def test_terminal_env_config_reads_docker_network_toggle(monkeypatch):
    monkeypatch.setenv("TERMINAL_DOCKER_NETWORK", "false")

    config = terminal_tool._get_env_config()

    assert config["docker_network"] is False


def test_sibling_container_config_sites_carry_docker_network():
    """Every container_config dict that carries docker_run_as_host_user must
    also carry docker_network — otherwise that code path silently falls back
    to networked containers while the terminal path honors the lockdown
    (the probe/exec asymmetry reported on issue #46358).
    """
    import ast
    import inspect

    import tools.code_execution_tool as code_execution_tool
    import tools.file_tools as file_tools

    for module in (terminal_tool, file_tools, code_execution_tool):
        tree = ast.parse(inspect.getsource(module))
        sites = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if "docker_run_as_host_user" in keys:
                sites += 1
                assert "docker_network" in keys, (
                    f"{module.__name__} builds a container_config with "
                    f"docker_run_as_host_user but without docker_network "
                    f"(line {node.lineno})"
                )
        assert sites >= 1, f"expected at least one container_config site in {module.__name__}"


def _reuse_guard_harness(
    monkeypatch,
    *,
    existing_mode: str,
    network: bool,
    existing_state: str = "running",
    commands=None,
):
    """Drive DockerEnvironment through the cross-process reuse path with a
    fake existing container whose NetworkMode is *existing_mode*.

    Returns the list of docker commands issued.
    """
    if commands is None:
        commands = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        if len(cmd) > 1 and cmd[1] == "ps":
            if 'Label "hermes-mounts"' in " ".join(cmd):
                # Stale-config probe: this harness exercises only network
                # reuse, so model no immutable-config mismatches.
                Result.stdout = ""
                return Result()
            # Matches the egress-aware reuse probe: with egress off the
            # format string is ID\tState\tEgressLabel and docker renders a
            # missing label as "<no value>".
            Result.stdout = (
                f"existing-container-id\t{existing_state}\t<no value>\n"
            )
        elif len(cmd) > 1 and cmd[1] == "inspect":
            Result.stdout = f"{existing_mode}\n"
        elif len(cmd) > 1 and cmd[1] == "run":
            Result.stdout = "fresh-container-id\n"
        return Result()

    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_env.DockerEnvironment, "_storage_opt_supported", lambda self: False)

    docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/workspace",
        timeout=60,
        task_id="reuse-guard-test",
        network=network,
        persist_across_processes=True,
    )
    return commands


@pytest.mark.parametrize(
    ("existing_mode", "network", "expected_network_arg"),
    [("bridge", False, "--network=none"), ("none", True, None)],
)
def test_reuse_removes_exited_network_mismatch_before_recreation(
    monkeypatch, existing_mode, network, expected_network_arg
):
    commands = _reuse_guard_harness(
        monkeypatch,
        existing_mode=existing_mode,
        network=network,
        existing_state="exited",
    )

    rm_cmd = next(cmd for cmd in commands if cmd[1] == "rm")
    assert rm_cmd[1:] == ["rm", "existing-container-id"]
    run_cmd = next(cmd for cmd in commands if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    if expected_network_arg is None:
        assert "--network=none" not in run_cmd
    else:
        assert expected_network_arg in run_cmd


@pytest.mark.parametrize(
    ("existing_mode", "network"), [("bridge", False), ("none", True)]
)
def test_reuse_blocks_running_network_mismatch_without_removal(
    monkeypatch, existing_mode, network
):
    commands = []
    with pytest.raises(RuntimeError, match="running Docker container"):
        _reuse_guard_harness(
            monkeypatch,
            existing_mode=existing_mode,
            network=network,
            commands=commands,
        )

    assert not any(cmd[1] == "rm" for cmd in commands)
    assert not any(cmd[1] == "run" for cmd in commands)


def test_reuse_keeps_airgapped_container_when_lockdown_requested(monkeypatch):
    commands = _reuse_guard_harness(monkeypatch, existing_mode="none", network=False)

    assert not any(cmd[1] == "rm" for cmd in commands)
    assert not any(cmd[1] == "run" for cmd in commands), "matching container must be reused"


def test_reuse_keeps_networked_container_when_network_enabled(monkeypatch):
    commands = _reuse_guard_harness(
        monkeypatch, existing_mode="bridge", network=True
    )

    assert any(cmd[1] == "inspect" for cmd in commands)
    assert not any(cmd[1] == "rm" for cmd in commands)
    assert not any(cmd[1] == "run" for cmd in commands)


def test_network_lockdown_reuses_legacy_container_without_network_label(monkeypatch):
    commands = []
    reuse_mounts = []
    network_filtered = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        if len(cmd) > 1 and cmd[1] == "ps":
            if 'Label "hermes-mounts"' in " ".join(cmd):
                return Result()
            mount_filter = next(
                part for part in cmd if str(part).startswith("label=hermes-mounts=")
            )
            reuse_mounts.append(mount_filter.rsplit("=", 1)[-1])
            network_filtered.append(
                f"label={docker_env._NETWORK_LABEL_KEY}=none" in cmd
            )
            if len(reuse_mounts) == 2:
                Result.stdout = "legacy-container-id\trunning\t<no value>\n"
        elif len(cmd) > 1 and cmd[1] == "inspect":
            Result.stdout = "none\n"
        return Result()

    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)
    monkeypatch.setattr(
        docker_env.DockerEnvironment,
        "_storage_opt_supported",
        lambda self: False,
    )

    env = docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/workspace",
        timeout=60,
        task_id="legacy-network-reuse",
        network=False,
        persist_across_processes=True,
    )

    assert len(reuse_mounts) == 2
    assert set(reuse_mounts) == {env._labels[docker_env._MOUNTS_LABEL_KEY]}
    assert network_filtered == [True, False]
    assert env._container_id == "legacy-container-id"
    assert not any(cmd[1] in {"rm", "run"} for cmd in commands)


def test_network_lockdown_removes_exited_legacy_bridge(monkeypatch):
    commands = []
    reuse_probes = 0

    def fake_run(cmd, *args, **kwargs):
        nonlocal reuse_probes
        commands.append(cmd)

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        if len(cmd) > 1 and cmd[1] == "ps":
            if 'Label "hermes-mounts"' in " ".join(cmd):
                return Result()
            reuse_probes += 1
            if reuse_probes == 2:
                Result.stdout = "old-cid\texited\t<no value>\n"
        elif len(cmd) > 1 and cmd[1] == "inspect":
            Result.stdout = "bridge\n"
        elif len(cmd) > 1 and cmd[1] == "run":
            Result.stdout = "new-cid\n"
        return Result()

    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)
    monkeypatch.setattr(
        docker_env.DockerEnvironment,
        "_storage_opt_supported",
        lambda self: False,
    )

    env = docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/workspace",
        timeout=60,
        task_id="legacy-network-exited",
        network=False,
        persist_across_processes=True,
    )

    assert env._container_id == "new-cid"
    rm_cmd = next(cmd for cmd in commands if cmd[1] == "rm")
    assert rm_cmd[1:] == ["rm", "old-cid"]
