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
import tarfile
import uuid
from contextlib import nullcontext
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

    def fetch_file_metadata_many(
        self, remote_paths: Iterable[str]
    ) -> dict[str, tuple[int, str] | None]: ...

    def fetch_file(
        self, remote_path: str, local_dest: str, max_bytes: int | None = None
    ) -> None: ...

    def put_file(self, local_source: str, remote_dest: str) -> None: ...

    def put_archive(self, local_archive: str, remote_dir: str) -> None: ...

    def publish_directory_atomic(self, source: str, destination: str) -> bool: ...

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
    return any(
        root == "/" or path == root or path.startswith(root + "/")
        for root in roots
    )


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
        self._container_generation = getattr(environment, "container_generation", None)

    def _artifact_session(self):
        session = getattr(self._environment, "artifact_session", None)
        if callable(session):
            return session(self._container_generation)
        return nullcontext()

    def _assert_generation(self) -> None:
        if self._container_generation is None:
            return
        current = getattr(self._environment, "container_generation", None)
        if current != self._container_generation:
            raise ArtifactTransferError(
                "container generation changed while the artifact handle was active "
                f"(expected {self._container_generation}, got {current})"
            )

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

    def _snapshot_host_source(self, source: Path) -> Path:
        """Copy one already-validated host file through a stable descriptor."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            source_fd = os.open(source, flags)
        except OSError as exc:
            raise ArtifactSecurityError(
                f"host artifact changed or became unavailable: {source!s}"
            ) from exc
        snapshot = self._cache_dir / (
            f".hermes-host-artifact-{uuid.uuid4().hex}.tmp"
        )
        destination_fd = -1
        completed = False
        try:
            opened = os.fstat(source_fd)
            current = os.lstat(source)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise ArtifactSecurityError(
                    f"host artifact changed after validation: {source!s}"
                )
            destination_fd = os.open(
                snapshot,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(source_fd, "rb", closefd=False) as reader, os.fdopen(
                destination_fd, "wb", closefd=False
            ) as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    writer.write(chunk)
            completed = True
            return snapshot
        except ArtifactSecurityError:
            raise
        except OSError as exc:
            raise ArtifactSecurityError(
                f"host artifact changed or became unavailable: {source!s}"
            ) from exc
        finally:
            os.close(source_fd)
            if destination_fd >= 0:
                os.close(destination_fd)
            if not completed:
                snapshot.unlink(missing_ok=True)

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

    def _ensure_container_directory(self, path: str) -> str:
        """Create *path* below a verified existing ancestor.

        Calling ``mkdir -p`` on the caller's lexical path first would follow
        a planted parent symlink and could create directories outside the
        allowed roots before the later realpath check noticed.
        """
        probe = self._guard_container_lexical(path)
        missing: list[str] = []
        while True:
            resolved = self._environment.fetch_realpath(probe)
            self._assert_generation()
            if resolved:
                canonical = _normalize_container_path(resolved)
                is_safe_ancestor = canonical == probe and any(
                    root.startswith(canonical.rstrip("/") + "/")
                    for root in self._container_roots
                )
                if not (
                    _is_within_container(canonical, self._container_roots)
                    or is_safe_ancestor
                ):
                    raise ArtifactSecurityError(
                        f"artifact is outside allowed container roots: {path}"
                    )
                break
            if probe == "/":
                raise ArtifactSecurityError(
                    f"could not resolve a safe container ancestor for: {path}"
                )
            missing.append(posixpath.basename(probe))
            probe = posixpath.dirname(probe) or "/"

        for component in reversed(missing):
            canonical = posixpath.join(canonical, component)
        canonical = self._guard_container_lexical(canonical)
        mkdir = self._environment.execute(
            f"mkdir -p -- {shlex.quote(canonical)}",
            rewrite_compound_background=False,
        )
        self._assert_generation()
        if int(mkdir.get("returncode") or 0) != 0:
            raise ArtifactTransferError(
                f"could not create container artifact directory: {path}"
            )
        return self._guard_container_resolved(canonical)

    @staticmethod
    def _verified(actual: tuple[int, str], expected: tuple[int, str]) -> bool:
        return actual[0] == expected[0] and actual[1].lower() == expected[1].lower()

    def pull(
        self,
        container_path: str,
        *,
        destination: str | Path | None = None,
        max_bytes: int | None = None,
    ) -> Path:
        with self._artifact_session():
            return self._pull(
                container_path, destination=destination, max_bytes=max_bytes
            )

    def _pull(
        self,
        container_path: str,
        *,
        destination: str | Path | None = None,
        max_bytes: int | None = None,
    ) -> Path:
        """Pull a regular file into the host cache and return its final path."""
        self._assert_generation()
        requested = self._guard_container_lexical(container_path)
        source = self._guard_container_resolved(requested)
        self._assert_generation()
        source_snapshot = posixpath.join(
            posixpath.dirname(source) or "/",
            f".{posixpath.basename(source)}.hermes-artifact-{uuid.uuid4().hex}.snapshot",
        )
        linked = self._environment.execute(
            f"ln -- {shlex.quote(source)} {shlex.quote(source_snapshot)}",
            rewrite_compound_background=False,
        )
        self._assert_generation()
        if int(linked.get("returncode") or 0) != 0:
            raise ArtifactTransferError(
                f"could not snapshot container artifact: {container_path!r}"
            )
        try:
            source_snapshot = self._guard_container_resolved(source_snapshot)
            before = self._environment.fetch_file_metadata(source_snapshot)
            self._assert_generation()
            if before is None:
                raise ArtifactTransferError(
                    f"container artifact is missing or not a regular file: {container_path}"
                )
            if max_bytes is not None and before[0] > max_bytes:
                raise ArtifactTransferError(
                    f"container artifact exceeds the {max_bytes}-byte transfer limit"
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
                if max_bytes is None:
                    self._environment.fetch_file(source_snapshot, str(temp_path))
                else:
                    self._environment.fetch_file(
                        source_snapshot, str(temp_path), max_bytes=max_bytes
                    )
                self._assert_generation()
                actual = _sha256(temp_path)
                after = self._environment.fetch_file_metadata(source_snapshot)
                self._assert_generation()
                if (
                    after is None
                    or before != after
                    or not self._verified(actual, before)
                    or (max_bytes is not None and actual[0] > max_bytes)
                ):
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
        finally:
            try:
                self._environment.execute(
                    f"rm -f -- {shlex.quote(source_snapshot)}",
                    rewrite_compound_background=False,
                )
            except Exception:
                pass

    def push(self, host_path: str | Path, container_path: str) -> None:
        with self._artifact_session():
            self._push(host_path, container_path)

    def _push(self, host_path: str | Path, container_path: str) -> None:
        """Push one host file and atomically publish it inside the environment."""
        self._assert_generation()
        source = self._guard_host_source(host_path)
        source_snapshot = self._snapshot_host_source(source)
        expected = _sha256(source_snapshot)
        destination = self._guard_container_lexical(container_path)
        parent = self._ensure_container_directory(
            posixpath.dirname(destination) or "/"
        )
        self._assert_generation()
        destination = posixpath.join(parent, posixpath.basename(destination))

        temp_path = posixpath.join(
            parent,
            f".{posixpath.basename(destination)}.hermes-artifact-{uuid.uuid4().hex}.tmp",
        )
        try:
            self._environment.put_file(str(source_snapshot), temp_path)
            self._assert_generation()
            uploaded = self._environment.fetch_file_metadata(temp_path)
            self._assert_generation()
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
            self._assert_generation()
            published = self._environment.fetch_file_metadata(destination)
            self._assert_generation()
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
            source_snapshot.unlink(missing_ok=True)
            try:
                self._environment.execute(
                    f"rm -f -- {shlex.quote(temp_path)}",
                    rewrite_compound_background=False,
                )
            except Exception:
                pass

    def push_tree(self, host_dir: str | Path, container_dir: str) -> None:
        with self._artifact_session():
            self._push_tree(host_dir, container_dir)

    def _push_tree(self, host_dir: str | Path, container_dir: str) -> None:
        """Publish one host directory with a single verified archive transfer."""
        self._assert_generation()
        source_root = Path(host_dir).expanduser().resolve(strict=True)
        if not source_root.is_dir() or not _is_within_host(
            source_root, self._host_roots
        ):
            raise ArtifactSecurityError(
                f"host artifact tree is outside allowed host roots: {host_dir!s}"
            )
        destination = self._guard_container_lexical(container_dir)
        parent = self._ensure_container_directory(
            posixpath.dirname(destination) or "/"
        )
        destination = posixpath.join(parent, posixpath.basename(destination))
        staging = posixpath.join(
            parent,
            f".{posixpath.basename(destination)}.hermes-tree-{uuid.uuid4().hex}.tmp",
        )
        archive_path = self._cache_dir / (
            f".hermes-host-tree-{uuid.uuid4().hex}.tar.tmp"
        )
        expected: dict[str, tuple[int, str]] = {}
        expected_relative: dict[str, tuple[int, str]] = {}
        published_live = False
        verified_live = False
        safe_to_cleanup_staging = True
        had_previous = False

        try:
            with tarfile.open(archive_path, mode="w") as archive:
                for root, dirnames, filenames in os.walk(source_root):
                    root_path = Path(root)
                    for dirname in dirnames:
                        if (root_path / dirname).is_symlink():
                            raise ArtifactSecurityError(
                                f"host artifact tree contains a symlink: "
                                f"{root_path / dirname!s}"
                            )
                    for filename in filenames:
                        source = root_path / filename
                        if source.is_symlink():
                            raise ArtifactSecurityError(
                                f"host artifact tree contains a symlink: {source!s}"
                            )
                        source = self._guard_host_source(source)
                        relative = source.relative_to(source_root).as_posix()
                        if any(ord(char) < 32 for char in relative):
                            raise ArtifactSecurityError(
                                "host artifact tree paths cannot contain control characters"
                            )
                        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                        descriptor = os.open(source, flags)
                        try:
                            opened = os.fstat(descriptor)
                            current = os.lstat(source)
                            if (
                                not stat.S_ISREG(opened.st_mode)
                                or not stat.S_ISREG(current.st_mode)
                                or (opened.st_dev, opened.st_ino)
                                != (current.st_dev, current.st_ino)
                            ):
                                raise ArtifactSecurityError(
                                    f"host artifact changed after validation: {source!s}"
                                )
                            digest = hashlib.sha256()
                            with os.fdopen(descriptor, "rb", closefd=False) as payload:
                                for chunk in iter(
                                    lambda: payload.read(1024 * 1024), b""
                                ):
                                    digest.update(chunk)
                                payload.seek(0)
                                member = tarfile.TarInfo(relative)
                                member.size = opened.st_size
                                member.mode = opened.st_mode & 0o777
                                member.mtime = int(opened.st_mtime)
                                archive.addfile(member, payload)
                            metadata = (
                                opened.st_size,
                                digest.hexdigest(),
                            )
                            expected_relative[relative] = metadata
                            expected[posixpath.join(destination, relative)] = metadata
                        finally:
                            os.close(descriptor)

            created = self._environment.execute(
                f"mkdir -m 700 -- {shlex.quote(staging)}",
                rewrite_compound_background=False,
            )
            if int(created.get("returncode") or 0) != 0:
                raise ArtifactTransferError(
                    f"could not create container tree staging directory: {container_dir}"
                )
            self._environment.put_archive(str(archive_path), staging)
            self._assert_generation()
            staging_expected = {
                posixpath.join(staging, relative): metadata
                for relative, metadata in expected_relative.items()
            }
            actual = self._environment.fetch_file_metadata_many(staging_expected)
            self._assert_generation()
            if actual != staging_expected:
                raise ArtifactTransferError(
                    f"artifact tree verification failed while pushing {host_dir!s}"
                )
            self._assert_generation()
            safe_to_cleanup_staging = False
            had_previous = self._environment.publish_directory_atomic(
                staging, destination
            )
            safe_to_cleanup_staging = True
            published_live = True
            self._assert_generation()
            final = self._environment.fetch_file_metadata_many(expected)
            self._assert_generation()
            if final != expected:
                raise ArtifactTransferError(
                    f"artifact tree verification failed after publishing {container_dir!r}"
                )
            verified_live = True
        except ArtifactTransferError as exc:
            if published_live and not verified_live:
                try:
                    safe_to_cleanup_staging = False
                    restored_previous = self._environment.publish_directory_atomic(
                        destination, staging
                    )
                    if restored_previous != had_previous:
                        raise ArtifactTransferError(
                            "artifact tree rollback state did not match publication"
                        )
                    safe_to_cleanup_staging = True
                except Exception as rollback_error:
                    raise ArtifactTransferError(
                        "artifact tree transfer failed and rollback failed "
                        f"for {container_dir!r}"
                    ) from rollback_error
            raise
        except Exception as exc:
            if published_live and not verified_live:
                try:
                    safe_to_cleanup_staging = False
                    restored_previous = self._environment.publish_directory_atomic(
                        destination, staging
                    )
                    if restored_previous != had_previous:
                        raise ArtifactTransferError(
                            "artifact tree rollback state did not match publication"
                        )
                    safe_to_cleanup_staging = True
                except Exception as rollback_error:
                    raise ArtifactTransferError(
                        "artifact tree transfer failed and rollback failed "
                        f"for {container_dir!r}"
                    ) from rollback_error
            raise ArtifactTransferError(
                f"could not push host artifact tree {host_dir!s}: {exc}"
            ) from exc
        finally:
            archive_path.unlink(missing_ok=True)
            if safe_to_cleanup_staging:
                try:
                    self._environment.execute(
                        f"rm -rf -- {shlex.quote(staging)}",
                        rewrite_compound_background=False,
                    )
                except Exception:
                    pass
