from unittest.mock import MagicMock


def _reset_registry(terminal_tool):
    terminal_tool._active_environments.clear()
    terminal_tool._last_activity.clear()
    terminal_tool._environment_generations.clear()
    terminal_tool._environment_leases.clear()
    terminal_tool._pending_environment_cleanup.clear()


def test_cleanup_is_deferred_until_artifact_lease_releases():
    from tools import terminal_tool

    _reset_registry(terminal_tool)
    env = MagicMock()
    lease = terminal_tool.acquire_environment_lease("task-1")
    terminal_tool._active_environments[lease.task_id] = env
    terminal_tool.cleanup_vm("task-1")

    assert terminal_tool.get_active_env(lease.task_id) is env
    env.cleanup.assert_not_called()

    lease.release()

    assert terminal_tool.get_active_env(lease.task_id) is None
    env.cleanup.assert_called_once()


def test_lease_can_be_acquired_before_lazy_environment_creation():
    from tools import terminal_tool

    _reset_registry(terminal_tool)
    lease = terminal_tool.acquire_environment_lease("task-lazy")
    env = MagicMock()
    terminal_tool._active_environments[lease.task_id] = env

    terminal_tool.cleanup_vm("task-lazy")
    assert terminal_tool.get_active_env(lease.task_id) is env

    lease.release()
    assert terminal_tool.get_active_env(lease.task_id) is None
    env.cleanup.assert_called_once()


def test_environment_generation_changes_when_registered():
    from tools import terminal_tool

    _reset_registry(terminal_tool)
    first = MagicMock()
    second = MagicMock()

    generation_1 = terminal_tool.register_active_environment("task-gen", first)
    terminal_tool.cleanup_vm("task-gen", force_remove=True)
    generation_2 = terminal_tool.register_active_environment("task-gen", second)

    assert generation_1 == 1
    assert generation_2 == 2
    assert terminal_tool.get_environment_generation("task-gen") == 2


def test_artifact_lease_release_is_idempotent():
    from tools import terminal_tool

    _reset_registry(terminal_tool)
    env = MagicMock()
    lease = terminal_tool.acquire_environment_lease("task-idempotent")
    terminal_tool._active_environments[lease.task_id] = env

    lease.release()
    lease.release()

    terminal_tool.cleanup_vm("task-idempotent")
    env.cleanup.assert_called_once()


def test_force_remove_waits_for_lease_and_preserves_force_intent():
    from tools import terminal_tool

    _reset_registry(terminal_tool)

    class _Environment:
        def __init__(self):
            self.cleanup_calls = []

        def cleanup(self, *, force_remove=False):
            self.cleanup_calls.append(force_remove)

    env = _Environment()
    lease = terminal_tool.acquire_environment_lease("task-force")
    terminal_tool._active_environments[lease.task_id] = env

    terminal_tool.cleanup_vm("task-force", force_remove=True)
    assert terminal_tool.get_active_env(lease.task_id) is env
    assert env.cleanup_calls == []

    lease.release()
    assert terminal_tool.get_active_env(lease.task_id) is None
    assert env.cleanup_calls == [True]


def test_inactive_reaper_marks_cleanup_pending_while_leased(monkeypatch):
    from tools import terminal_tool

    _reset_registry(terminal_tool)
    env = MagicMock()
    lease = terminal_tool.acquire_environment_lease("task-idle")
    terminal_tool._active_environments[lease.task_id] = env
    terminal_tool._last_activity[lease.task_id] = 0
    monkeypatch.setattr(terminal_tool.time, "time", lambda: 1000)

    terminal_tool._cleanup_inactive_envs(lifetime_seconds=1)
    assert terminal_tool.get_active_env(lease.task_id) is env

    lease.release()
    assert terminal_tool.get_active_env(lease.task_id) is None
    env.cleanup.assert_called_once()
