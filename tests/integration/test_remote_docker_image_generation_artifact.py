"""Opt-in proof that generated host images reach a remote Docker container."""

import hashlib
import json
import os
import subprocess
import uuid

import pytest

from tools import image_generation_tool
from tools.environments import docker as docker_env


pytestmark = pytest.mark.skipif(
    os.getenv("HERMES_TEST_REMOTE_DOCKER") != "1",
    reason="set HERMES_TEST_REMOTE_DOCKER=1 with a disposable remote daemon",
)


def test_generated_image_is_bridged_into_remote_docker(tmp_path, monkeypatch):
    assert os.getenv("DOCKER_HOST", "").startswith("ssh://")
    profile_home = tmp_path / "profile"
    image_dir = profile_home / "cache" / "images"
    image_dir.mkdir(parents=True)
    image_path = image_dir / "generated.png"
    image_path.write_bytes(b"real generated image bytes")

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr("tools.credential_files.get_credential_file_mounts", lambda: [])
    monkeypatch.setattr("tools.credential_files.get_skills_directory_mount", lambda: [])
    monkeypatch.setattr(
        docker_env, "_get_active_profile_name", lambda: "remote-image-integration"
    )
    monkeypatch.setattr(
        docker_env, "_egress_proxy_args_for_docker", lambda: ([], {}, [])
    )
    monkeypatch.setattr(docker_env, "_cgroup_limits_available", lambda _image: False)

    env = docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/workspace",
        task_id=f"remote-image-{uuid.uuid4().hex}",
        persistent_filesystem=False,
        persist_across_processes=False,
        network=False,
        shm_size="0",
    )
    container_id = env._container_id
    assert container_id
    try:
        assert env.remote_endpoint is True
        monkeypatch.setattr(
            image_generation_tool, "_active_terminal_env", lambda _task_id: env
        )

        result = json.loads(
            image_generation_tool._postprocess_image_generate_result(
                json.dumps({"success": True, "image": str(image_path)}),
                task_id="remote-image-task",
            )
        )

        agent_path = "/root/.hermes/cache/images/generated.png"
        expected = (
            image_path.stat().st_size,
            hashlib.sha256(image_path.read_bytes()).hexdigest(),
        )
        assert result["agent_visible_image"] == agent_path
        assert env.fetch_file_metadata(agent_path) == expected
    finally:
        subprocess.run(
            [env._docker_exe, "rm", "-f", container_id],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
