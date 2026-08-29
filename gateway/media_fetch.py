"""Fetch MEDIA files out of a remote terminal backend for platform delivery.

``MEDIA:/absolute/path`` only delivers when the file exists on the machine
running the gateway. Under a remote terminal backend (docker, ssh, modal,
daytona, ...) the agent's artifacts live on a different filesystem, so
``validate_media_delivery_path`` rejects them and the attachment never
arrives (issues #466, #75065).

This module bridges the gap: when a MEDIA path is missing locally and a
remote backend is active, the file is fetched through that backend's
``fetch_file`` transport into the canonical document cache
(``<HERMES_HOME>/cache/documents``, already a delivery-safe root) and
delivered from there.

Two guards, both deliberate:

* The *remote* path is checked against the same credential / system-path
  denylist as local delivery BEFORE any bytes move, so a fetch can never
  become a bypass of ``_path_under_denied_prefix``. Symlinks are resolved
  inside the backend and re-checked.
* Only a locally-*missing* path is fetched. A path rejected as denied stays
  rejected: re-fetching it from the backend would launder the rejection.
"""

import logging
import os
import posixpath
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# Maximum size fetched out of a backend for delivery. Telegram bot uploads
# cap at 50 MB and other platforms are the same order of magnitude, so a
# larger fetch would burn the transfer and then fail at the send anyway.
MEDIA_FETCH_MAX_BYTES_ENV = "HERMES_MEDIA_FETCH_MAX_BYTES"
_MEDIA_FETCH_MAX_BYTES_DEFAULT = 50 * 1024 * 1024


def acquire_media_delivery_lease(
    task_id: Optional[str] = None,
    task_id_factory: Optional[Callable[[], Optional[str]]] = None,
):
    """Keep a remote task environment alive through attachment delivery.

    The lease is acquired before the terminal environment necessarily exists;
    this closes the turn-finalizer race with lazy sandbox creation. Callers
    must release the returned object after media paths have been pulled into
    the host cache. Local turns need no lease and return ``None``.
    """
    backend = (os.getenv("TERMINAL_ENV") or "local").strip().lower()
    from agent.prompt_builder import _REMOTE_TERMINAL_BACKENDS

    if backend not in _REMOTE_TERMINAL_BACKENDS:
        return None
    if not task_id and task_id_factory is not None:
        task_id = task_id_factory()
    if not task_id:
        return None
    from tools.terminal_tool import acquire_environment_lease

    return acquire_environment_lease(task_id)


def stage_inbound_media(
    paths: list[str], task_id: Optional[str]
) -> list[tuple[str, str]]:
    """Push host-cached inbound files into a remote Docker container.

    Returns ``(basename, reason)`` entries for files that could not be staged.
    Local Docker uses bind mounts and therefore needs no transfer.
    """
    backend = (os.getenv("TERMINAL_ENV") or "local").strip().lower()
    if backend != "docker" or not task_id or not paths:
        return []

    from tools.credential_files import (
        from_agent_visible_cache_path,
        get_cache_directory_mounts,
        to_agent_visible_cache_path,
    )
    from tools.environments.artifact_bridge import ArtifactBridge
    from tools.terminal_tool import ensure_task_env

    try:
        env = ensure_task_env(task_id)
    except Exception as exc:  # noqa: BLE001 — convert startup failure to a note
        logger.warning("Remote Docker environment creation failed: %s", exc)
        env = None
    if env is None:
        return [
            (Path(from_agent_visible_cache_path(str(path))).name or "file",
             "remote Docker environment unavailable")
            for path in paths
        ]
    if not bool(getattr(env, "remote_endpoint", False)):
        return []

    mounts = get_cache_directory_mounts()
    failures: list[tuple[str, str]] = []
    for raw_path in paths:
        host_path = Path(from_agent_visible_cache_path(str(raw_path)))
        container_path = to_agent_visible_cache_path(str(host_path))
        mount = next(
            (
                entry
                for entry in mounts
                if container_path == entry["container_path"]
                or container_path.startswith(entry["container_path"] + "/")
            ),
            None,
        )
        if mount is None or container_path == str(host_path):
            failures.append((host_path.name or "file", "not in a managed media cache"))
            continue
        try:
            bridge = ArtifactBridge(
                env,
                cache_dir=mount["host_path"],
                host_roots=(mount["host_path"],),
                container_roots=(mount["container_path"],),
            )
            bridge.push(host_path, container_path)
        except Exception as exc:  # noqa: BLE001 — report all staging failures
            logger.warning("Inbound media staging failed for %s: %s", host_path.name, exc)
            failures.append((host_path.name or "file", str(exc)))
    return failures


def media_fetch_max_bytes() -> int:
    """Return the configured remote-fetch size cap in bytes."""
    raw = os.environ.get(MEDIA_FETCH_MAX_BYTES_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _MEDIA_FETCH_MAX_BYTES_DEFAULT


def _format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} bytes"


def remote_path_is_denied(path: str, remote_home: Optional[str] = None) -> bool:
    """Return True when *path* on a backend filesystem must not be fetched.

    Pure string check (the remote filesystem cannot be stat'd from here)
    applying the same denylist as local delivery: system prefixes (``/etc``,
    ``/proc``, ...), credential directories under the backend home
    (``~/.ssh``, ``~/.aws``, ...), and the Hermes credential stores
    (``~/.hermes/.env``, ``auth.json``, ``mcp-tokens/``, ...).

    When *remote_home* is unknown, home-relative entries are matched against
    ANY path component (conservative: ``/data/.ssh/key`` is denied too).
    Mirrors the ``/root``-is-home exception of ``_path_under_denied_prefix``:
    a denied system prefix that IS the backend's own home stays fetchable,
    since its credential subpaths are separate, more-specific entries.
    """
    from gateway.platforms.base import (
        _MEDIA_DELIVERY_DENIED_HOME_SUBPATHS,
        _MEDIA_DELIVERY_DENIED_PREFIXES,
        _ROOT_CREDENTIAL_DIRS,
        _ROOT_CREDENTIAL_FILES,
    )

    normalized = PurePosixPath(posixpath.normpath(path))
    if not normalized.is_absolute():
        return True

    home = PurePosixPath(posixpath.normpath(remote_home)) if remote_home else None

    def _under(candidate: PurePosixPath, root: PurePosixPath) -> bool:
        return candidate == root or root in candidate.parents

    for prefix in _MEDIA_DELIVERY_DENIED_PREFIXES:
        root = PurePosixPath(prefix)
        if home is not None and root == home:
            continue
        if _under(normalized, root):
            return True

    home_relative = list(_MEDIA_DELIVERY_DENIED_HOME_SUBPATHS)
    home_relative.extend(
        posixpath.join(".hermes", *PurePosixPath(rel.replace(os.sep, "/")).parts)
        for rel in (*_ROOT_CREDENTIAL_FILES, *_ROOT_CREDENTIAL_DIRS)
    )

    if home is not None:
        for rel in home_relative:
            if _under(normalized, home / rel):
                return True
        return False

    # Unknown home: deny when a denied subpath appears anywhere in the path.
    parts = normalized.parts
    for rel in home_relative:
        rel_parts = PurePosixPath(rel.replace(os.sep, "/")).parts
        for start in range(len(parts) - len(rel_parts) + 1):
            if parts[start:start + len(rel_parts)] == rel_parts:
                return True
    return False


def _active_remote_environment(task_id: Optional[str] = None):
    """Return ``(backend_name, env)`` when a remote terminal backend is active.

    ``backend`` is ``""`` when the configured backend is local (nothing to
    fetch from). ``env`` is None when the backend is remote but no live
    session exists — with an ephemeral sandbox the artifact is gone with it,
    so this is a real "cannot deliver", not something to retry.

    The gateway keys terminal environments by session id. ``get_active_env``
    already collapses that id onto ``"default"`` in shared-container mode;
    an explicit session miss must not fall back to another session's sandbox.
    """
    backend = (os.getenv("TERMINAL_ENV") or "local").strip().lower()
    from agent.prompt_builder import _REMOTE_TERMINAL_BACKENDS

    if backend not in _REMOTE_TERMINAL_BACKENDS:
        return "", None
    try:
        from tools.terminal_tool import get_active_env

        return backend, get_active_env(task_id or "default")
    except Exception as exc:
        logger.debug("Remote media fetch: could not resolve active env: %s", exc)
        return backend, None


def _sanitize_basename(path: str) -> str:
    name = posixpath.basename(posixpath.normpath(path.replace("\\", "/")))
    name = "".join(c for c in name if c.isprintable() and c not in '/\\:*?"<>|')
    return name.strip() or "file"


def fetch_remote_media(
    path: str,
    task_id: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Fetch *path* from the active remote backend into the document cache.

    Returns ``(local_path, None)`` on success, where *local_path* has already
    passed ``validate_media_delivery_path``, or ``(None, reason)`` with a
    short reason for the log. Never raises: a bug here must not break the
    delivery of the message itself.
    """
    from gateway.platforms.base import (
        get_document_cache_dir,
        validate_media_delivery_path,
    )
    from tools.environments.artifact_bridge import ArtifactBridge, ArtifactTransferError

    backend, env = _active_remote_environment(task_id)
    if not backend:
        return None, "no remote terminal backend is active"
    if env is None:
        return None, f"no active {backend} terminal session to fetch from"

    candidate = posixpath.normpath(str(path).strip())
    remote_home = env.remote_home
    if candidate == "~" or candidate.startswith("~/"):
        if not remote_home:
            return None, "cannot resolve ~ in the backend"
        candidate = posixpath.normpath(posixpath.join(remote_home, candidate[2:]))
    if not candidate.startswith("/"):
        return None, "only absolute paths can be fetched from the backend"

    if remote_path_is_denied(candidate, remote_home):
        return None, "the path is not allowed for delivery"

    try:
        # Best-effort symlink resolution so a link planted at an innocuous
        # path cannot smuggle out a denied credential file.
        resolved = env.fetch_realpath(candidate)
        if resolved and remote_path_is_denied(resolved, remote_home):
            return None, "the path is not allowed for delivery"

        size = env.fetch_file_size(resolved or candidate)
        if size is None:
            return None, (
                "the file is not in the agent sandbox (it was never created, "
                "or it was written to a different path)"
            )
        limit = max_bytes or media_fetch_max_bytes()
        if size > limit:
            return None, (
                f"the file is {_format_size(size)}, above the "
                f"{_format_size(limit)} delivery limit"
            )

        cache_dir = get_document_cache_dir()
        dest = cache_dir / (
            f"doc_{uuid.uuid4().hex[:12]}_{_sanitize_basename(candidate)}"
        )
        remote_source = resolved or candidate
        bridge = ArtifactBridge(
            env,
            cache_dir=cache_dir,
            host_roots=(cache_dir,),
            container_roots=(posixpath.dirname(remote_source) or "/",),
        )
        bridge.pull(remote_source, destination=dest, max_bytes=limit)
    except ArtifactTransferError as exc:
        return None, str(exc)
    except Exception as exc:
        logger.warning(
            "Remote media fetch failed for %s backend: %s", backend, exc,
            exc_info=True,
        )
        return None, f"fetching from the {backend} backend failed"

    validated = validate_media_delivery_path(str(dest))
    if not validated:
        try:
            Path(dest).unlink()
        except OSError:
            pass
        return None, "the fetched file failed delivery validation"

    logger.info(
        "Fetched remote media from %s backend: %s (%s)",
        backend, candidate, _format_size(size),
    )
    return validated, None
