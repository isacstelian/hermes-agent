#!/usr/bin/env python3
"""Relay one weekly Hermes failure to the CTO dispatcher's internal register."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime

REMOTE_COMMAND = """
set -e
tool_dir="$HOME/.t3/tools"
dispatcher=$(python3 -I "$tool_dir/role-thread-ids.py" current cto-dispatcher)
exec python3 -I "$tool_dir/dispatch-send.py" --sender-thread "$1" "$dispatcher" -
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sender-thread", required=True)
    args = parser.parse_args()
    event = json.load(sys.stdin)
    job_id = event["job_id"]
    execution_id = event["execution_id"]
    if not all(re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value) for value in (job_id, execution_id)):
        parser.error("invalid cron execution identifiers")

    clock = datetime.now().astimezone().strftime("%H:%M %Z")
    line = (
        f"[DE ȘTIUT] {clock} · Cron săptămânal Hermes pe iMac eșuat. "
        f"Job {job_id}, rulare {execution_id}. "
        f"Dovadă: ~/.hermes/cron/output/{job_id}/ pe iMac.\n"
    )
    remote = shlex.join(["/bin/sh", "-c", REMOTE_COMMAND, "weekly-cron", args.sender_thread])
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "agent-linux", remote],
        input=line, text=True, capture_output=True, timeout=15,
    )
    if result.returncode != 0 or not result.stdout.startswith("noted: "):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
