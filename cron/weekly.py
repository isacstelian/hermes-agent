"""Profile-local policy for weekly agent cron jobs."""
from __future__ import annotations

import json
import subprocess

from hermes_cli.fallback_config import get_weekly_provider_policy


def weekly_policy(job: dict, config: dict) -> dict:
    from cron.scheduler import _job_interval_minutes

    policy = get_weekly_provider_policy(config)
    if not policy or _job_interval_minutes(job) != 7 * 24 * 60:
        return {}
    script_ids = policy.get("chat_script_job_ids") or []
    if job.get("no_agent") and job.get("id") not in script_ids:
        return {}
    return policy


def alert_command(policy: dict) -> list[str]:
    command = policy.get("failure_command")
    if isinstance(command, list) and command and all(
        isinstance(arg, str) and arg for arg in command
    ):
        return command
    return []


def dispatch_failure(command: list[str], job_id: str, execution_id: str) -> str | None:
    """Send a stable run ID, never model output, credentials or stack traces."""
    from tools.environments.local import build_subprocess_env

    try:
        subprocess.run(
            command,
            input=json.dumps({"job_id": job_id, "execution_id": execution_id}) + "\n",
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=build_subprocess_env(),
            timeout=20,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "Weekly cron failure dispatch failed"
    return None
