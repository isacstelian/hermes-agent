from __future__ import annotations

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override

from agent.browser_context import (
    bind_browser_context,
    clear_browser_context,
    get_browser_context,
)


def test_context_binding_is_scoped_by_task_id() -> None:
    bind_browser_context("task-a", "context-a")
    bind_browser_context("task-b", "context-b")
    try:
        assert get_browser_context("task-a") == "context-a"
        assert get_browser_context("task-b") == "context-b"
        assert get_browser_context("task-c") is None
    finally:
        clear_browser_context("task-a")
        clear_browser_context("task-b")


def test_context_binding_rejects_blank_values() -> None:
    with pytest.raises(ValueError):
        bind_browser_context("", "context-a")
    with pytest.raises(ValueError):
        bind_browser_context("task-a", "")


def test_clear_does_not_affect_other_tasks() -> None:
    bind_browser_context("task-a", "context-a")
    bind_browser_context("task-b", "context-b")
    try:
        clear_browser_context("task-a")
        assert get_browser_context("task-a") is None
        assert get_browser_context("task-b") == "context-b"
    finally:
        clear_browser_context("task-b")


def test_context_binding_is_scoped_by_profile(tmp_path) -> None:
    first = set_hermes_home_override(tmp_path / "profile-a")
    try:
        bind_browser_context("shared-task", "context-a")
        assert get_browser_context("shared-task") == "context-a"
    finally:
        reset_hermes_home_override(first)

    second = set_hermes_home_override(tmp_path / "profile-b")
    try:
        assert get_browser_context("shared-task") is None
        bind_browser_context("shared-task", "context-b")
        assert get_browser_context("shared-task") == "context-b"
        clear_browser_context("shared-task")
    finally:
        reset_hermes_home_override(second)

    first = set_hermes_home_override(tmp_path / "profile-a")
    try:
        assert get_browser_context("shared-task") == "context-a"
        clear_browser_context("shared-task")
    finally:
        reset_hermes_home_override(first)
