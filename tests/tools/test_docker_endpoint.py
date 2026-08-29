import subprocess

import pytest

from tools.environments import docker as docker_env


@pytest.mark.parametrize(
    ("endpoint", "remote"),
    [
        ("ssh://sandbox-vps", True),
        ("tcp://10.20.0.3:2376", True),
        ("unix:///var/run/docker.sock", False),
        ("npipe:////./pipe/docker_engine", False),
    ],
)
def test_explicit_docker_host_classification(monkeypatch, endpoint, remote):
    monkeypatch.setenv("DOCKER_HOST", endpoint)

    assert docker_env.docker_endpoint_is_remote("docker") is remote


def test_default_context_is_local_without_inspection(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "default")
    monkeypatch.setattr(
        docker_env.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("default context needs no inspection"),
    )

    assert docker_env.docker_endpoint_is_remote("docker") is False


def test_explicit_context_overrides_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-board")
    monkeypatch.setattr(
        docker_env.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, stdout="ssh://board-host\n", stderr=""
        ),
    )

    assert docker_env.docker_endpoint_is_remote("docker") is True


@pytest.mark.parametrize(
    ("endpoint", "remote"),
    [
        ("ssh://sandbox-vps", True),
        ("tcp://10.20.0.3:2376", True),
        ("unix:///Users/magic/.docker/run/docker.sock", False),
    ],
)
def test_named_context_uses_its_effective_endpoint(
    monkeypatch, endpoint, remote
):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "board-sandbox")

    def _run(cmd, **kwargs):
        assert cmd[1:4] == ["context", "inspect", "board-sandbox"]
        return subprocess.CompletedProcess(cmd, 0, stdout=endpoint + "\n", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", _run)

    assert docker_env.docker_endpoint_is_remote("docker") is remote


def test_uninspectable_named_context_fails_closed(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "unknown-remote")
    monkeypatch.setattr(
        docker_env.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="missing"
        ),
    )

    assert docker_env.docker_endpoint_is_remote("docker") is True


def test_current_context_probe_nonzero_fails_closed(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(
        docker_env.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="daemon unavailable"
        ),
    )

    assert docker_env.docker_endpoint_is_remote("docker") is True


def test_current_context_probe_timeout_fails_closed(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(
        docker_env.subprocess,
        "run",
        lambda cmd, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd, 10)
        ),
    )

    assert docker_env.docker_endpoint_is_remote("docker") is True


def test_local_podman_does_not_probe_docker_context(monkeypatch):
    for name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "CONTAINER_HOST",
        "CONTAINER_CONNECTION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        docker_env.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("local Podman has no Docker context"),
    )

    assert docker_env.docker_endpoint_is_remote("/usr/bin/podman") is False


@pytest.mark.parametrize("variable", ["CONTAINER_HOST", "CONTAINER_CONNECTION"])
def test_explicit_podman_remote_environment_is_remote(monkeypatch, variable):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    monkeypatch.delenv("CONTAINER_CONNECTION", raising=False)
    monkeypatch.setenv(variable, "ssh://board-host" if variable.endswith("HOST") else "board")
    monkeypatch.setattr(
        docker_env.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("explicit Podman remote needs no probe"),
    )

    assert docker_env.docker_endpoint_is_remote("podman") is True


def test_local_podman_unix_service_is_local(monkeypatch):
    monkeypatch.setenv(
        "CONTAINER_HOST", "unix:///run/user/1000/podman/podman.sock"
    )
    monkeypatch.delenv("CONTAINER_CONNECTION", raising=False)

    assert docker_env.docker_endpoint_is_remote("podman") is False


def test_local_podman_docker_compatibility_shim_is_local(monkeypatch, tmp_path):
    shim = tmp_path / "docker"
    shim.write_text("#!/bin/sh\nexec podman \"$@\"\n")
    for name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "CONTAINER_HOST",
        "CONTAINER_CONNECTION",
    ):
        monkeypatch.delenv(name, raising=False)

    def _run(cmd, **kwargs):
        assert cmd == [str(shim), "--version"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout="podman version 5.5.2\n", stderr=""
        )

    monkeypatch.setattr(docker_env.subprocess, "run", _run)

    assert docker_env.docker_endpoint_is_remote(str(shim)) is False
