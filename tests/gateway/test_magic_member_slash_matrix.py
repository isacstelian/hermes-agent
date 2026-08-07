"""Contract matrix for Magic's member-safe Telegram slash-command batch.

Magic PA pins this Hermes commit and enables the canonical commands only in
member DMs. These tests keep canonicalization and DM/group authorization from
drifting underneath that profile configuration.
"""

from __future__ import annotations

import pytest

from gateway.slash_access import policy_from_extra
from hermes_cli.commands import resolve_command


MEMBER_DM_COMMANDS = (
    "new",
    "status",
    "usage",
    "stop",
    "undo",
    "goal",
    "retry",
    "title",
    "branch",
    "compress",
    "queue",
    "steer",
    "subgoal",
    "commands",
    "version",
    "background",
    "reload-skills",
)

MEMBER_DM_ALIASES = {
    "reset": "new",
    "fork": "branch",
    "compact": "compress",
    "q": "queue",
    "v": "version",
    "bg": "background",
    "btw": "background",
    "reload_skills": "reload-skills",
}

EXTRA = {
    "allow_admin_from": ["111"],
    "user_allowed_commands": list(MEMBER_DM_COMMANDS),
    "group_allow_admin_from": ["111"],
    "group_user_allowed_commands": [],
}


@pytest.mark.parametrize("command", MEMBER_DM_COMMANDS)
def test_non_admin_member_can_run_each_reviewed_command_in_dm(command: str) -> None:
    policy = policy_from_extra(EXTRA, "dm")

    assert policy.can_run("999", command)


@pytest.mark.parametrize(("alias", "canonical"), MEMBER_DM_ALIASES.items())
def test_reviewed_aliases_resolve_before_dm_authorization(
    alias: str, canonical: str
) -> None:
    resolved = resolve_command(alias)
    policy = policy_from_extra(EXTRA, "dm")

    assert resolved is not None
    assert resolved.name == canonical
    assert policy.can_run("999", resolved.name)


@pytest.mark.parametrize("command", MEMBER_DM_COMMANDS)
def test_non_admin_member_cannot_run_reviewed_dm_commands_in_groups(
    command: str,
) -> None:
    policy = policy_from_extra(EXTRA, "group")

    assert not policy.can_run("999", command)


@pytest.mark.parametrize("command", ("restart", "update", "debug", "platform"))
def test_non_batch_commands_remain_denied_to_non_admin_members_in_dm(
    command: str,
) -> None:
    policy = policy_from_extra(EXTRA, "dm")

    assert not policy.can_run("999", command)
