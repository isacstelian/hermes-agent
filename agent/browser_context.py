"""Process-local binding of browser tasks to persistent cloud contexts.

Bindings are installed only after a host integration has verified the caller's
identity. Cloud providers consume the opaque context id; they never infer an
identity from a task/session string. Bindings are also scoped by the active
Hermes profile so multiplexed runtimes cannot cross profile boundaries.
"""

from __future__ import annotations

import threading

from hermes_constants import get_hermes_home

_lock = threading.RLock()
_bindings: dict[tuple[str, str], str] = {}


def _binding_key(task_id: str) -> tuple[str, str] | None:
    task = str(task_id or "").strip()
    if not task:
        return None
    profile_home = str(get_hermes_home().expanduser().resolve())
    return profile_home, task


def bind_browser_context(task_id: str, context_id: str) -> None:
    """Bind one trusted browser task to one cloud context."""

    key = _binding_key(task_id)
    context = str(context_id or "").strip()
    if key is None or not context:
        raise ValueError("task_id and context_id are required")
    with _lock:
        _bindings[key] = context


def get_browser_context(task_id: str) -> str | None:
    """Return the trusted context binding for ``task_id``, if any."""

    key = _binding_key(task_id)
    if key is None:
        return None
    with _lock:
        return _bindings.get(key)


def clear_browser_context(task_id: str) -> None:
    """Remove a task binding without affecting other users/tasks."""

    key = _binding_key(task_id)
    if key is None:
        return
    with _lock:
        _bindings.pop(key, None)
