"""Behavior contracts for the bidirectional artifact bridge."""

import hashlib
import os
import shlex
import tarfile
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
        self.archive_destinations = []
        self.directory_publications = []
        self.commands = []
        self.metadata_calls = []
        self.metadata_many_calls = []

    def fetch_realpath(self, path):
        return self.realpaths.get(path, path)

    def fetch_file_metadata(self, path):
        self.metadata_calls.append(path)
        payload = self.files.get(path)
        if payload is None:
            return None
        return len(payload), hashlib.sha256(payload).hexdigest()

    def fetch_file_metadata_many(self, paths):
        paths = list(paths)
        self.metadata_many_calls.append(paths)
        return {
            path: (
                (len(self.files[path]), hashlib.sha256(self.files[path]).hexdigest())
                if path in self.files else None
            )
            for path in paths
        }

    def fetch_file(self, path, destination, max_bytes=None):
        self.fetch_destinations.append(Path(destination))
        payload = self.files[path]
        if max_bytes is not None and len(payload) > max_bytes:
            raise FileFetchError("artifact exceeds transfer limit")
        Path(destination).write_bytes(payload)

    def put_file(self, source, destination):
        self.put_destinations.append(destination)
        self.files[destination] = Path(source).read_bytes()

    def put_archive(self, source, destination):
        self.archive_destinations.append(destination)
        with tarfile.open(source, "r") as archive:
            for member in archive.getmembers():
                if member.isfile():
                    payload = archive.extractfile(member)
                    assert payload is not None
                    self.files[f"{destination}/{member.name}"] = payload.read()

    def publish_directory_atomic(self, source, destination):
        self.directory_publications.append((source, destination))
        had_previous = any(
            path == destination or path.startswith(destination + "/")
            for path in self.files
        )
        source_files = {
            destination + path.removeprefix(source): payload
            for path, payload in self.files.items()
            if path == source or path.startswith(source + "/")
        }
        destination_files = {
            source + path.removeprefix(destination): payload
            for path, payload in self.files.items()
            if path == destination or path.startswith(destination + "/")
        }
        for path in list(self.files):
            if (
                path == source
                or path.startswith(source + "/")
                or path == destination
                or path.startswith(destination + "/")
            ):
                self.files.pop(path)
        self.files.update(source_files)
        if had_previous:
            self.files.update(destination_files)
        self.realpaths[destination] = destination
        return had_previous

    def execute(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("mkdir -p "):
            self.realpaths[shlex.split(command)[-1]] = shlex.split(command)[-1]
            return {"returncode": 0, "output": ""}
        if command.startswith("mkdir -m 700 -- "):
            self.realpaths[shlex.split(command)[-1]] = shlex.split(command)[-1]
            return {"returncode": 0, "output": ""}
        if command.startswith("ln -- "):
            source, destination = shlex.split(command)[2:4]
            self.files[destination] = self.files[source]
            return {"returncode": 0, "output": ""}
        if command.startswith("mv -f -- "):
            source, destination = command.removeprefix("mv -f -- ").split(" ", 1)
            self.files[destination] = self.files.pop(source)
            return {"returncode": 0, "output": ""}
        if command.startswith("rm -f -- "):
            self.files.pop(command.removeprefix("rm -f -- "), None)
            return {"returncode": 0, "output": ""}
        if command.startswith("rm -rf -- "):
            prefixes = shlex.split(command)[3:]
            for prefix in prefixes:
                for path in list(self.files):
                    if path == prefix or path.startswith(prefix + "/"):
                        self.files.pop(path)
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


def test_pull_snapshots_source_before_independent_transfer_steps(tmp_path):
    class RacingEnvironment(_FakeEnvironment):
        def execute(self, command, **kwargs):
            result = super().execute(command, **kwargs)
            if command.startswith("ln -- "):
                self.files["/workspace/blob"] = b"SECRET-OUTSIDE-ROOT"
            return result

    env = RacingEnvironment({"/workspace/blob": b"safe-report"})
    bridge, _inbox, _cache = _bridge(tmp_path, env)

    destination = bridge.pull("/workspace/blob")

    assert destination.read_bytes() == b"safe-report"


def test_pull_enforces_limit_inside_transport(tmp_path):
    env = _FakeEnvironment({"/workspace/blob": b"12345"})
    bridge, _inbox, cache = _bridge(tmp_path, env)

    with pytest.raises(ArtifactTransferError, match="limit"):
        bridge.pull("/workspace/blob", max_bytes=4)

    assert list(cache.iterdir()) == []


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


def test_push_tree_batches_files_and_atomically_publishes(tmp_path):
    env = _FakeEnvironment()
    bridge, inbox, cache = _bridge(tmp_path, env)
    tree = inbox / "skills"
    (tree / "report" / "scripts").mkdir(parents=True)
    (tree / "report" / "SKILL.md").write_text("# Report")
    (tree / "report" / "scripts" / "run.py").write_bytes(b"print('ok')")

    bridge.push_tree(tree, "/workspace/skills")

    assert len(env.archive_destinations) == 1
    assert env.files["/workspace/skills/report/SKILL.md"] == b"# Report"
    assert env.files["/workspace/skills/report/scripts/run.py"] == b"print('ok')"
    assert not any("hermes-tree" in path for path in env.files)
    assert list(cache.iterdir()) == []


def test_push_tree_532_files_has_constant_transport_call_budget(tmp_path):
    env = _FakeEnvironment()
    bridge, inbox, cache = _bridge(tmp_path, env)
    tree = inbox / "skills"
    tree.mkdir()
    expected = {}
    for index in range(532):
        path = tree / f"skill-{index:03d}.bin"
        payload = f"payload-{index}".encode()
        path.write_bytes(payload)
        expected[f"/workspace/skills/{path.name}"] = payload

    bridge.push_tree(tree, "/workspace/skills")

    assert env.archive_destinations and len(env.archive_destinations) == 1
    assert len(env.metadata_many_calls) == 2
    assert all(len(paths) == 532 for paths in env.metadata_many_calls)
    assert env.put_destinations == []
    assert env.metadata_calls == []
    assert len(env.directory_publications) == 1
    assert {path: env.files[path] for path in expected} == expected
    assert list(cache.iterdir()) == []


def test_push_tree_hash_mismatch_rejects_before_publication_and_cleans(tmp_path):
    class CorruptStagingEnvironment(_FakeEnvironment):
        def fetch_file_metadata_many(self, paths):
            result = super().fetch_file_metadata_many(paths)
            first = next(iter(result))
            result[first] = (0, "0" * 64)
            return result

    old_path = "/workspace/skills/existing.bin"
    env = CorruptStagingEnvironment({old_path: b"old"})
    bridge, inbox, cache = _bridge(tmp_path, env)
    tree = inbox / "skills"
    tree.mkdir()
    (tree / "new.bin").write_bytes(b"new")

    with pytest.raises(ArtifactTransferError, match="verification failed"):
        bridge.push_tree(tree, "/workspace/skills")

    assert env.files[old_path] == b"old"
    assert "/workspace/skills/new.bin" not in env.files
    assert not any("hermes-tree" in path for path in env.files)
    assert list(cache.iterdir()) == []


def test_push_tree_final_hash_mismatch_rolls_back_previous_tree(tmp_path):
    class CorruptFinalEnvironment(_FakeEnvironment):
        def fetch_file_metadata_many(self, paths):
            result = super().fetch_file_metadata_many(paths)
            if len(self.metadata_many_calls) == 2:
                first = next(iter(result))
                result[first] = (0, "0" * 64)
            return result

    old_path = "/workspace/skills/existing.bin"
    env = CorruptFinalEnvironment({old_path: b"old"})
    bridge, inbox, cache = _bridge(tmp_path, env)
    tree = inbox / "skills"
    tree.mkdir()
    (tree / "new.bin").write_bytes(b"new")

    with pytest.raises(ArtifactTransferError, match="verification failed"):
        bridge.push_tree(tree, "/workspace/skills")

    assert env.files == {old_path: b"old"}
    assert len(env.directory_publications) == 2
    assert list(cache.iterdir()) == []


def test_push_tree_interruption_after_atomic_exchange_keeps_complete_trees(tmp_path):
    class InterruptedEnvironment(_FakeEnvironment):
        def publish_directory_atomic(self, source, destination):
            result = super().publish_directory_atomic(source, destination)
            raise ArtifactTransferError("interrupted after atomic exchange")

    old_path = "/workspace/skills/existing.bin"
    env = InterruptedEnvironment({old_path: b"old"})
    bridge, inbox, cache = _bridge(tmp_path, env)
    tree = inbox / "skills"
    tree.mkdir()
    (tree / "new.bin").write_bytes(b"new")

    with pytest.raises(ArtifactTransferError, match="interrupted"):
        bridge.push_tree(tree, "/workspace/skills")

    assert env.files["/workspace/skills/new.bin"] == b"new"
    retained_old = [
        payload
        for path, payload in env.files.items()
        if "hermes-tree" in path and path.endswith("/existing.bin")
    ]
    assert retained_old == [b"old"]
    assert list(cache.iterdir()) == []


def test_push_tree_failed_atomic_rollback_retains_both_complete_trees(tmp_path):
    class FailedRollbackEnvironment(_FakeEnvironment):
        def fetch_file_metadata_many(self, paths):
            result = super().fetch_file_metadata_many(paths)
            if len(self.metadata_many_calls) == 2:
                first = next(iter(result))
                result[first] = (0, "0" * 64)
            return result

        def publish_directory_atomic(self, source, destination):
            if self.directory_publications:
                raise ArtifactTransferError("rollback interrupted")
            return super().publish_directory_atomic(source, destination)

    old_path = "/workspace/skills/existing.bin"
    env = FailedRollbackEnvironment({old_path: b"old"})
    bridge, inbox, cache = _bridge(tmp_path, env)
    tree = inbox / "skills"
    tree.mkdir()
    (tree / "new.bin").write_bytes(b"new")

    with pytest.raises(ArtifactTransferError, match="rollback failed"):
        bridge.push_tree(tree, "/workspace/skills")

    assert env.files["/workspace/skills/new.bin"] == b"new"
    retained_old = [
        payload
        for path, payload in env.files.items()
        if "hermes-tree" in path and path.endswith("/existing.bin")
    ]
    assert retained_old == [b"old"]
    assert list(cache.iterdir()) == []


def test_push_tree_rejects_symlinks_before_transport(tmp_path):
    env = _FakeEnvironment()
    bridge, inbox, _cache = _bridge(tmp_path, env)
    tree = inbox / "skills"
    tree.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("secret")
    (tree / "link").symlink_to(outside)

    with pytest.raises(ArtifactSecurityError, match="symlink"):
        bridge.push_tree(tree, "/workspace/skills")

    assert env.archive_destinations == []


def test_push_creates_missing_allowed_root_below_safe_ancestor(tmp_path):
    env = _FakeEnvironment(realpaths={
        "/root/.hermes/cache/documents": None,
        "/root/.hermes/cache": None,
        "/root/.hermes": None,
        "/root": "/root",
    })
    inbox = tmp_path / "inbox"
    cache = tmp_path / "cache"
    inbox.mkdir()
    cache.mkdir()
    source = inbox / "report.pdf"
    source.write_bytes(b"%PDF")
    bridge = ArtifactBridge(
        env,
        cache_dir=cache,
        host_roots=(inbox,),
        container_roots=("/root/.hermes/cache/documents",),
    )

    bridge.push(source, "/root/.hermes/cache/documents/report.pdf")

    assert env.files["/root/.hermes/cache/documents/report.pdf"] == b"%PDF"


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


def test_push_rejects_source_replaced_by_symlink_after_validation(tmp_path, monkeypatch):
    env = _FakeEnvironment()
    bridge, inbox, _cache = _bridge(tmp_path, env)
    source = inbox / "payload.bin"
    source.write_bytes(b"safe")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"SECRET-OUTSIDE-HOST-ROOT")
    original_guard = bridge._guard_host_source

    def racing_guard(path):
        resolved = original_guard(path)
        resolved.unlink()
        resolved.symlink_to(outside)
        return resolved

    monkeypatch.setattr(bridge, "_guard_host_source", racing_guard)

    with pytest.raises(ArtifactSecurityError, match="changed|unavailable"):
        bridge.push(source, "/workspace/payload.bin")

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
    assert "shasum -a 256" in env.calls[0][0]
    assert "openssl dgst -sha256" in env.calls[0][0]


def test_base_put_file_has_explicit_last_resort_cap(tmp_path):
    source = tmp_path / "large"
    source.write_bytes(b"x" * 17)
    env = _FakeBaseEnvironment([])
    env._base64_transfer_limit_bytes = 16

    with pytest.raises(FileFetchError, match="fallback limit"):
        env.put_file(str(source), "/workspace/large")

    assert env.calls == []
