"""Tests for terminal truncation spill + metadata (deferred retrieval)."""

import json
import os
from pathlib import Path

import pytest

from tools.terminal_tool import terminal_tool


@pytest.fixture
def small_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    import tools.tool_output_limits as lim
    monkeypatch.setattr(lim, "_cached_limits", {
        "max_bytes": 2000, "max_lines": 2000, "max_line_length": 2000,
    })
    return tmp_path


class TestTruncationSpill:
    def test_truncated_output_has_metadata_and_spill(self, small_cap):
        r = json.loads(terminal_tool(
            "python3 -c \"print('marker_head'); [print(f'row_{i}', 'x'*80) for i in range(200)]; print('marker_tail')\"",
            task_id="t-spill-1"))
        assert r["exit_code"] == 0
        assert "OUTPUT TRUNCATED" in r["output"]
        assert r["output_total_chars"] > 2000
        p = Path(r["full_output_path"])
        assert p.exists()
        full = p.read_text()
        assert "marker_head" in full and "marker_tail" in full
        # The spill contains rows that were cut from the visible window.
        assert "row_100 " in full
        assert "read_file" in r["truncation_note"]

    def test_small_output_has_no_metadata(self, small_cap):
        r = json.loads(terminal_tool("echo tiny", task_id="t-spill-2"))
        assert r["exit_code"] == 0
        assert "full_output_path" not in r
        assert "output_total_chars" not in r

    def test_spill_is_redacted(self, small_cap):
        r = json.loads(terminal_tool(
            "python3 -c \"print('sk-proj-' + 'a1B2c3D4e5F6g7H8i9J0' * 3); [print('pad', 'y'*90) for i in range(200)]\"",
            task_id="t-spill-3"))
        p = Path(r["full_output_path"])
        full = p.read_text()
        assert "a1B2c3D4e5F6g7H8i9J0a1B2c3D4e5F6g7H8i9J0" not in full

    def test_old_spills_cleaned(self, small_cap, tmp_path):
        spill_dir = tmp_path / ".hermes" / "cache" / "terminal-output"
        spill_dir.mkdir(parents=True, exist_ok=True)
        stale = spill_dir / "out-1-2-dead.log"
        stale.write_text("old")
        os.utime(stale, (1, 1))
        json.loads(terminal_tool(
            "python3 -c \"[print('z'*90) for i in range(200)]\"", task_id="t-spill-4"))
        assert not stale.exists()

    def test_failed_command_still_gets_spill(self, small_cap):
        r = json.loads(terminal_tool(
            "python3 -c \"[print('e'*90) for i in range(200)]; import sys; sys.exit(3)\"",
            task_id="t-spill-5"))
        assert r["exit_code"] == 3
        assert Path(r["full_output_path"]).exists()


def test_sandbox_terminal_spill_handle(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    import tools.terminal_tool as tt
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spill = tmp_path / "cache/terminal-output/output.log"
    spill.parent.mkdir(parents=True)
    spill.write_text("complete output")
    env = MagicMock()
    env.cwd = "/root"
    env.execute.return_value = {
        "output": "head...tail", "returncode": 0,
        "full_output_path": str(spill), "output_total_chars": 10000,
    }
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(tt, "_check_all_guards", lambda *a, **kw: {"approved": True})
    monkeypatch.setitem(tt._active_environments, "default", env)
    for backend in ("local", "singularity", "docker"):
        monkeypatch.setenv("TERMINAL_ENV", backend)
        config = {
            "env_type": backend, "timeout": 30, "cwd": "/root", "host_cwd": None,
            "modal_mode": "auto", "docker_image": "", "singularity_image": "",
            "modal_image": "", "daytona_image": "",
        }
        monkeypatch.setattr(tt, "_get_env_config", lambda: config)
        result = json.loads(tt.terminal_tool("echo hello"))
        expected = ("/root/.hermes/cache/terminal-output/output.log"
                    if backend == "docker" else str(spill))
        assert result.get("full_output_path") == expected, result
        assert expected in result["truncation_note"]
        assert spill.read_text() == "complete output"
