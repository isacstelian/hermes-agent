"""Behavior contracts for the bidirectional artifact bridge."""

import hashlib
import os
from pathlib import Path

import pytest

from tools.environments.artifact_bridge import (
    ArtifactBridge,
    ArtifactSecurityError,
    ArtifactTransferError,
)
from tools.environments.base import BaseEnvironment, FileFetchError


class _FakeEnvironment:
    def __init__(self, files=None, realpaths=None):
        self.container_generation = 1
        self.files = dict(files or {})
        self.realpaths = dict(realpaths or {})
        self.fetch_destinations = []
        self.put_destinations = []
        self.commands = []

    def fetch_realpath(self, path):
        return self.realpaths.get(path, path)

    def fetch_file_metadata(self, path):
        payload = self.files.get(path)
        if payload is None:
            return None
        return len(payload), hashlib.sha256(payload).hexdigest()

    def fetch_file(self, path, destination):
        self.fetch_destinations.append(Path(destination))
        Path(destination).write_bytes(self.files[path])

    def put_file(self, source, destination):
        self.put_destinations.append(destination)
        self.files[destination] = Path(source).read_bytes()

    def execute(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("mkdir -p "):
            return {"returncode": 0, "output": ""}
        if command.startswith("mv -f -- "):
            source, destination = command.removeprefix("mv -f -- ").split(" ", 1)
            self.files[destination] = self.files.pop(source)
            return {"returncode": 0, "output": ""}
        if command.startswith("rm -f -- "):
            self.files.pop(command.removeprefix("rm -f -- "), None)
            return {"returncode": 0, "output": ""}
        raise AssertionError(f"unexpected command: {command}")


class _FakeBaseEnvironment(BaseEnvironment):
    def __init__(self, results):
        self.timeout = 60
        self.results = list(results)
        self.calls = []

    def execute(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.results.pop(0)

    def cleanup(self):
        pass


def _bridge(tmp_path, env):
    inbox = tmp_path / "inbox"
    cache = tmp_path / "cache"
    inbox.mkdir()
    cache.mkdir()
    return ArtifactBridge(
        env,
        cache_dir=cache,
        host_roots=(inbox,),
        container_roots=("/workspace", "/root"),
    ), inbox, cache


def test_pull_is_byte_agnostic_verified_and_atomically_published(tmp_path):
    payload = b"\x00\xffPK\x03\x04not-an-extension"
    env = _FakeEnvironment({"/workspace/blob": payload})
    bridge, _inbox, cache = _bridge(tmp_path, env)

    destination = bridge.pull("/workspace/blob")

    assert destination.parent == cache
    assert destination.read_bytes() == payload
    assert len(env.fetch_destinations) == 1
    assert env.fetch_destinations[0] != destination
    assert not env.fetch_destinations[0].exists()


def test_pull_rejects_container_symlink_escape(tmp_path):
    env = _FakeEnvironment(
        {"/workspace/report": b"secret"},
        realpaths={"/workspace/report": "/etc/passwd"},
    )
    bridge, _inbox, _cache = _bridge(tmp_path, env)

    with pytest.raises(ArtifactSecurityError, match="outside allowed container roots"):
        bridge.pull("/workspace/report")

    assert env.fetch_destinations == []


def test_pull_hash_mismatch_does_not_publish_destination(tmp_path):
    env = _FakeEnvironment({"/workspace/report": b"before"})
    bridge, _inbox, cache = _bridge(tmp_path, env)

    def corrupting_fetch(_path, destination):
        env.fetch_destinations.append(Path(destination))
        Path(destination).write_bytes(b"after")

    env.fetch_file = corrupting_fetch

    with pytest.raises(ArtifactTransferError, match="verification failed"):
        bridge.pull("/workspace/report")

    assert list(cache.iterdir()) == []


def test_push_uses_remote_temp_verifies_then_renames(tmp_path):
    env = _FakeEnvironment()
    bridge, inbox, _cache = _bridge(tmp_path, env)
    source = inbox / "payload.bin"
    source.write_bytes(b"arbitrary\x00bytes")

    bridge.push(source, "/workspace/uploads/payload.bin")

    assert env.files["/workspace/uploads/payload.bin"] == source.read_bytes()
    assert len(env.put_destinations) == 1
    assert env.put_destinations[0] != "/workspace/uploads/payload.bin"
    assert not any(".hermes-artifact-" in path for path in env.files)
    assert any(command.startswith("mv -f -- ") for command in env.commands)


def test_push_rejects_host_symlink_escape(tmp_path):
    env = _FakeEnvironment()
    bridge, inbox, _cache = _bridge(tmp_path, env)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    link = inbox / "link.bin"
    link.symlink_to(outside)

    with pytest.raises(ArtifactSecurityError, match="outside allowed host roots"):
        bridge.push(link, "/workspace/link.bin")

    assert env.put_destinations == []


def test_push_rejects_lexical_container_traversal(tmp_path):
    env = _FakeEnvironment()
    bridge, inbox, _cache = _bridge(tmp_path, env)
    source = inbox / "payload"
    source.write_bytes(b"x")

    with pytest.raises(ArtifactSecurityError, match="outside allowed container roots"):
        bridge.push(source, "/workspace/../etc/passwd")


def test_push_does_not_mkdir_through_escaped_container_symlink(tmp_path):
    env = _FakeEnvironment(
        realpaths={
            "/workspace/link/new": None,
            "/workspace/link": "/etc",
        }
    )
    bridge, inbox, _cache = _bridge(tmp_path, env)
    source = inbox / "payload"
    source.write_bytes(b"x")

    with pytest.raises(ArtifactSecurityError, match="outside allowed container roots"):
        bridge.push(source, "/workspace/link/new/payload")

    assert env.commands == []


def test_pull_refuses_to_overwrite_cache_through_symlink(tmp_path):
    env = _FakeEnvironment({"/workspace/report": b"report"})
    bridge, _inbox, cache = _bridge(tmp_path, env)
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = cache / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactSecurityError, match="outside allowed host roots"):
        bridge.pull("/workspace/report", destination=redirected / "report")

    assert not (outside / "report").exists()


def test_bridge_requires_explicit_containment_roots(tmp_path):
    with pytest.raises(ValueError, match="host_roots"):
        ArtifactBridge(
            _FakeEnvironment(),
            cache_dir=tmp_path,
            host_roots=(),
            container_roots=("/workspace",),
        )

    with pytest.raises(ValueError, match="container_roots"):
        ArtifactBridge(
            _FakeEnvironment(),
            cache_dir=tmp_path,
            host_roots=(tmp_path,),
            container_roots=(),
        )


def test_bridge_rejects_stale_container_generation(tmp_path):
    env = _FakeEnvironment({"/workspace/report": b"report"})
    bridge, _inbox, _cache = _bridge(tmp_path, env)
    env.container_generation = 2

    with pytest.raises(ArtifactTransferError, match="container generation changed"):
        bridge.pull("/workspace/report")

    assert env.fetch_destinations == []


def test_pull_detects_recreation_during_transfer(tmp_path):
    env = _FakeEnvironment({"/workspace/report": b"report"})
    bridge, _inbox, cache = _bridge(tmp_path, env)

    def recreating_fetch(path, destination):
        Path(destination).write_bytes(env.files[path])
        env.container_generation += 1

    env.fetch_file = recreating_fetch

    with pytest.raises(ArtifactTransferError, match="container generation changed"):
        bridge.pull("/workspace/report")

    assert list(cache.iterdir()) == []


def test_base_metadata_returns_size_and_sha256_from_one_probe():
    digest = "a" * 64
    env = _FakeBaseEnvironment(
        [{"returncode": 0, "output": f"login noise\n123 {digest}\n"}]
    )

    assert env.fetch_file_metadata("/workspace/blob") == (123, digest)
    assert len(env.calls) == 1


def test_base_put_file_has_explicit_last_resort_cap(tmp_path):
    source = tmp_path / "large"
    source.write_bytes(b"x" * 17)
    env = _FakeBaseEnvironment([])
    env._base64_transfer_limit_bytes = 16

    with pytest.raises(FileFetchError, match="fallback limit"):
        env.put_file(str(source), "/workspace/large")

    assert env.calls == []
