"""Verified, atomic artifact transfer between a host and an environment.

The bridge owns policy (containment, symlink resolution, verification and
atomic publication).  Environment backends own only the byte transport.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import shlex
import stat
import uuid
from pathlib import Path
from typing import Iterable, Protocol


class ArtifactTransferError(RuntimeError):
    """An artifact could not be transferred or verified."""


class ArtifactSecurityError(ArtifactTransferError):
    """An artifact path escaped an explicitly allowed root."""


class ArtifactEnvironment(Protocol):
    """Minimal environment surface consumed by :class:`ArtifactBridge`."""

    def fetch_realpath(self, remote_path: str) -> str | None: ...

    def fetch_file_metadata(self, remote_path: str) -> tuple[int, str] | None: ...

    def fetch_file(self, remote_path: str, local_dest: str) -> None: ...

    def put_file(self, local_source: str, remote_dest: str) -> None: ...

    def execute(self, command: str, **kwargs) -> dict: ...


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _is_within_host(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _normalize_container_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ArtifactSecurityError("artifact container path must be absolute")
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        raise ArtifactSecurityError("artifact container path must be absolute")
    return normalized


def _is_within_container(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


class ArtifactBridge:
    """Push and pull files through an environment with fail-closed guards."""

    def __init__(
        self,
        environment: ArtifactEnvironment,
        *,
        cache_dir: str | Path,
        host_roots: Iterable[str | Path],
        container_roots: Iterable[str],
    ) -> None:
        host = tuple(Path(root).expanduser().resolve(strict=True) for root in host_roots)
        if not host:
            raise ValueError("host_roots must contain at least one directory")
        if any(not root.is_dir() for root in host):
            raise ValueError("every host_roots entry must be a directory")

        container = tuple(
            dict.fromkeys(_normalize_container_path(root) for root in container_roots)
        )
        if not container:
            raise ValueError("container_roots must contain at least one directory")

        cache = Path(cache_dir).expanduser()
        cache.mkdir(parents=True, exist_ok=True)
        cache = cache.resolve(strict=True)
        if not cache.is_dir():
            raise ValueError("cache_dir must be a directory")

        self._environment = environment
        self._cache_dir = cache
        self._host_roots = host
        self._container_roots = container

    def _guard_host_source(self, path: str | Path) -> Path:
        try:
            resolved = Path(path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ArtifactSecurityError(f"host artifact is unavailable: {path!s}") from exc
        if not _is_within_host(resolved, self._host_roots):
            raise ArtifactSecurityError(
                f"host artifact is outside allowed host roots: {path!s}"
            )
        try:
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise ArtifactSecurityError(f"host artifact is unavailable: {path!s}") from exc
        if not stat.S_ISREG(mode):
            raise ArtifactSecurityError(f"host artifact is not a regular file: {path!s}")
        return resolved

    def _guard_host_destination(self, path: Path) -> Path:
        try:
            resolved = path.expanduser().resolve(strict=False)
        except OSError as exc:
            raise ArtifactSecurityError(f"host destination is unavailable: {path!s}") from exc
        if not _is_within_host(resolved, (self._cache_dir,)):
            raise ArtifactSecurityError(
                f"host destination is outside allowed host roots: {path!s}"
            )
        return resolved

    def _guard_container_lexical(self, path: str) -> str:
        normalized = _normalize_container_path(path)
        if not _is_within_container(normalized, self._container_roots):
            raise ArtifactSecurityError(
                f"artifact is outside allowed container roots: {path}"
            )
        return normalized

    def _guard_container_resolved(self, path: str) -> str:
        resolved = self._environment.fetch_realpath(path)
        if not resolved:
            raise ArtifactSecurityError(f"could not resolve container path: {path}")
        resolved = _normalize_container_path(resolved)
        if not _is_within_container(resolved, self._container_roots):
            raise ArtifactSecurityError(
                f"artifact is outside allowed container roots: {path}"
            )
        return resolved

    @staticmethod
    def _verified(actual: tuple[int, str], expected: tuple[int, str]) -> bool:
        return actual[0] == expected[0] and actual[1].lower() == expected[1].lower()

    def pull(
        self,
        container_path: str,
        *,
        destination: str | Path | None = None,
    ) -> Path:
        """Pull a regular file into the host cache and return its final path."""
        requested = self._guard_container_lexical(container_path)
        source = self._guard_container_resolved(requested)
        before = self._environment.fetch_file_metadata(source)
        if before is None:
            raise ArtifactTransferError(
                f"container artifact is missing or not a regular file: {container_path}"
            )

        if destination is None:
            name = posixpath.basename(requested) or "artifact"
            destination_path = self._cache_dir / f"{uuid.uuid4().hex}-{name}"
        else:
            destination_path = Path(destination)
        destination_path = self._guard_host_destination(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path = self._guard_host_destination(destination_path)

        temp_path = destination_path.parent / (
            f".{destination_path.name}.hermes-artifact-{uuid.uuid4().hex}.tmp"
        )
        descriptor = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(descriptor)
        try:
            self._environment.fetch_file(source, str(temp_path))
            actual = _sha256(temp_path)
            after = self._environment.fetch_file_metadata(source)
            if after is None or before != after or not self._verified(actual, before):
                raise ArtifactTransferError(
                    f"artifact verification failed while pulling {container_path!r}"
                )
            os.replace(temp_path, destination_path)
            return destination_path
        except ArtifactTransferError:
            raise
        except Exception as exc:
            raise ArtifactTransferError(
                f"could not pull container artifact {container_path!r}: {exc}"
            ) from exc
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def push(self, host_path: str | Path, container_path: str) -> None:
        """Push one host file and atomically publish it inside the environment."""
        source = self._guard_host_source(host_path)
        expected = _sha256(source)
        destination = self._guard_container_lexical(container_path)
        parent = posixpath.dirname(destination) or "/"

        mkdir = self._environment.execute(
            f"mkdir -p -- {shlex.quote(parent)}",
            rewrite_compound_background=False,
        )
        if int(mkdir.get("returncode") or 0) != 0:
            raise ArtifactTransferError(
                f"could not create container artifact directory: {parent}"
            )
        self._guard_container_resolved(parent)

        temp_path = posixpath.join(
            parent,
            f".{posixpath.basename(destination)}.hermes-artifact-{uuid.uuid4().hex}.tmp",
        )
        try:
            self._environment.put_file(str(source), temp_path)
            uploaded = self._environment.fetch_file_metadata(temp_path)
            if uploaded is None or not self._verified(uploaded, expected):
                raise ArtifactTransferError(
                    f"artifact verification failed while pushing {host_path!s}"
                )
            moved = self._environment.execute(
                f"mv -f -- {shlex.quote(temp_path)} {shlex.quote(destination)}",
                rewrite_compound_background=False,
            )
            if int(moved.get("returncode") or 0) != 0:
                raise ArtifactTransferError(
                    f"could not publish container artifact: {container_path}"
                )
            published = self._environment.fetch_file_metadata(destination)
            if published is None or not self._verified(published, expected):
                raise ArtifactTransferError(
                    f"artifact verification failed after publishing {container_path!r}"
                )
        except ArtifactTransferError:
            raise
        except Exception as exc:
            raise ArtifactTransferError(
                f"could not push host artifact {host_path!s}: {exc}"
            ) from exc
        finally:
            try:
                self._environment.execute(
                    f"rm -f -- {shlex.quote(temp_path)}",
                    rewrite_compound_background=False,
                )
            except Exception:
                pass
