"""Process-local binding of browser tasks to persistent cloud contexts.

Bindings are installed only after a host integration has verified the caller's
identity. Cloud providers consume the opaque context id; they never infer an
identity from a task/session string.
"""

from __future__ import annotations

import threading

_lock = threading.RLock()
_bindings: dict[str, str] = {}


def bind_browser_context(task_id: str, context_id: str) -> None:
    """Bind one trusted browser task to one cloud context."""

    task = str(task_id or "").strip()
    context = str(context_id or "").strip()
    if not task or not context:
        raise ValueError("task_id and context_id are required")
    with _lock:
        _bindings[task] = context


def get_browser_context(task_id: str) -> str | None:
    """Return the trusted context binding for ``task_id``, if any."""

    task = str(task_id or "").strip()
    if not task:
        return None
    with _lock:
        return _bindings.get(task)


def clear_browser_context(task_id: str) -> None:
    """Remove a task binding without affecting other users/tasks."""

    task = str(task_id or "").strip()
    if not task:
        return
    with _lock:
        _bindings.pop(task, None)
