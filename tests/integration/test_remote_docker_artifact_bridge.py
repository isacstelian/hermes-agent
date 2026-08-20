"""Opt-in integration test for a real remote Docker daemon."""

import hashlib
import json
import os
import shlex
import subprocess
import uuid

import pytest

from tools.environments import docker as docker_env
from tools.environments.artifact_bridge import ArtifactBridge


pytestmark = pytest.mark.skipif(
    os.getenv("HERMES_TEST_REMOTE_DOCKER") != "1",
    reason="set HERMES_TEST_REMOTE_DOCKER=1 with a disposable remote daemon",
)


def test_real_ssh_daemon_round_trips_docx_and_large_pdf(tmp_path, monkeypatch):
    assert os.getenv("DOCKER_HOST", "").startswith("ssh://")
    monkeypatch.setattr(
        docker_env.DockerEnvironment,
        "_stage_remote_auto_inputs",
        lambda self: None,
    )
    monkeypatch.setattr(
        docker_env, "_get_active_profile_name", lambda: "remote-artifact-integration"
    )
    monkeypatch.setattr(
        docker_env, "_egress_proxy_args_for_docker", lambda: ([], {}, [])
    )
    monkeypatch.setattr(
        docker_env, "_cgroup_limits_available", lambda _image: False
    )

    env = docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/workspace",
        task_id=f"remote-artifact-{uuid.uuid4().hex}",
        persistent_filesystem=False,
        persist_across_processes=False,
        network=False,
        shm_size="0",
    )
    container_id = env._container_id
    assert container_id
    try:
        assert env.remote_endpoint is True
        inspected = subprocess.run(
            [
                env._docker_exe,
                "inspect",
                "--format",
                "{{json .Mounts}}",
                container_id,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert all(mount.get("Type") != "bind" for mount in json.loads(inspected.stdout))

        inbound = tmp_path / "Raport board.docx"
        inbound.write_bytes(b"PK\x03\x04real telegram word bytes")
        cache = tmp_path / "cache"
        bridge = ArtifactBridge(
            env,
            cache_dir=cache,
            host_roots=[tmp_path],
            container_roots=["/workspace"],
        )

        bridge.push(inbound, "/workspace/inbound.docx")
        expected_docx = (
            inbound.stat().st_size,
            hashlib.sha256(inbound.read_bytes()).hexdigest(),
        )
        assert env.fetch_file_metadata("/workspace/inbound.docx") == expected_docx

        payload_size = 9 * 1024 * 1024
        script = (
            "from pathlib import Path; "
            f"Path('/workspace/outbound.pdf').write_bytes(b'%PDF-1.7\\n' + b'x' * {payload_size})"
        )
        created = env.execute(
            f"python -c {shlex.quote(script)}",
            rewrite_compound_background=False,
        )
        assert created["returncode"] == 0, created

        pulled = bridge.pull(
            "/workspace/outbound.pdf",
            max_bytes=10 * 1024 * 1024,
        )
        data = pulled.read_bytes()
        assert data.startswith(b"%PDF-1.7\n")
        assert len(data) == payload_size + len(b"%PDF-1.7\n")
    finally:
        subprocess.run(
            [env._docker_exe, "rm", "-f", container_id],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
