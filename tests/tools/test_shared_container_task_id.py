"""
Regression tests for the shared-container task_id mapping.

The top-level agent and all delegate_task subagents share a single
terminal sandbox keyed by ``"default"``.  ``_resolve_container_task_id``
is the sole gatekeeper for which tool-call task_ids go to the shared
container vs. get their own isolated sandbox.  RL / benchmark
environments opt in to isolation by calling
``register_task_env_overrides(task_id, {...})`` before the agent loop;
every other task_id collapses back to ``"default"``.

If you change the collapse logic, update both the helper and these
tests -- see `hermes-agent-dev` skill, "Why do subagents get their own
containers?" section, and the Container lifecycle paragraph under
Docker Backend in ``website/docs/user-guide/configuration.md``.
"""

import pytest
from unittest.mock import MagicMock

from tools import terminal_tool


@pytest.fixture(autouse=True)
def _clean_overrides():
    """Ensure no stray overrides from other tests leak in."""
    before = dict(terminal_tool._task_env_overrides)
    terminal_tool._task_env_overrides.clear()
    yield
    terminal_tool._task_env_overrides.clear()
    terminal_tool._task_env_overrides.update(before)


def test_none_task_id_maps_to_default():
    assert terminal_tool._resolve_container_task_id(None) == "default"


def test_empty_task_id_maps_to_default():
    assert terminal_tool._resolve_container_task_id("") == "default"


def test_cwd_only_override_collapses_to_default():
    """CWD-only overrides (ACP adapter workspace tracking) must NOT trigger
    container isolation — they should collapse to the shared 'default'
    container so all surfaces (TUI, gateway, dashboard) share one sandbox.
    Regression for #37361."""
    terminal_tool.register_task_env_overrides(
        "acp-session-abc", {"cwd": "/home/user/project"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("acp-session-abc")
            == "default"
        )
    finally:
        terminal_tool.clear_task_env_overrides("acp-session-abc")


def test_env_type_override_keeps_own_id():
    """env_type is an isolation key — must trigger per-task container."""
    terminal_tool.register_task_env_overrides(
        "bench-env", {"env_type": "sandbox", "cwd": "/work"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("bench-env")
            == "bench-env"
        )
    finally:
        terminal_tool.clear_task_env_overrides("bench-env")


@pytest.mark.parametrize("task_id", ["sa-1-abcd1234", "session-close", "cron-close"])
def test_collapsed_task_cleanup_preserves_shared_environment(monkeypatch, task_id):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {
        "env_type": "docker", "container_persistent": True,
    })
    env = MagicMock(_container_id="parent-container")
    monkeypatch.setattr(terminal_tool, "_active_environments", {"default": env})
    monkeypatch.setattr(terminal_tool, "_last_activity", {"default": 123})
    monkeypatch.setattr(terminal_tool, "_pending_environment_cleanup", {})
    monkeypatch.setattr(terminal_tool, "_environment_leases", {"default": 1})
    creation_lock = object()
    monkeypatch.setattr(terminal_tool, "_creation_locks", {"default": creation_lock})

    terminal_tool.cleanup_vm(task_id)

    assert terminal_tool.get_active_env(task_id) is env
    assert env._container_id == "parent-container"
    assert terminal_tool._last_activity["default"] == 123
    assert terminal_tool._creation_locks["default"] is creation_lock
    assert terminal_tool._pending_environment_cleanup == {}
    env.cleanup.assert_not_called()

    # A child must not leave deferred teardown for an artifact lease release.
    terminal_tool._environment_leases.clear()
    terminal_tool.cleanup_vm(task_id)
    assert terminal_tool.get_active_env(task_id) is env
    env.cleanup.assert_not_called()

    terminal_tool.cleanup_vm(task_id, force_remove=True)
    assert terminal_tool.get_active_env(task_id) is None
    env.cleanup.assert_called_once()
