"""Durable per-execution delivery and routine-feedback behavior."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    return executions


def test_execution_context_outcome_and_duration_are_persisted(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    current = [datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(executions, "_hermes_now", lambda: current[0])

    claimed = executions.create_execution(
        "daily-brief",
        source="builtin",
        job_name="Daily brief",
        definition_hash="sha256:original",
    )
    assert claimed["job_name"] == "Daily brief"
    assert claimed["definition_hash"] == "sha256:original"
    assert claimed["delivery_outcome"] is None
    assert claimed["delivery_error"] is None
    assert claimed["duration_ms"] is None

    current[0] += timedelta(seconds=2)
    executions.mark_execution_running(claimed["id"])
    updated = executions.update_execution_context(
        claimed["id"],
        job_name="Daily executive brief",
        definition_hash="sha256:claimed-definition",
    )
    assert updated["job_name"] == "Daily executive brief"
    assert updated["definition_hash"] == "sha256:claimed-definition"

    current[0] += timedelta(seconds=3, milliseconds=250)
    completed = executions.finish_execution(
        claimed["id"], success=True, delivery_outcome="delivered"
    )

    assert completed["delivery_outcome"] == "delivered"
    assert completed["duration_ms"] == 3250
    assert executions.latest_execution("daily-brief") == completed


def test_successful_execution_keeps_delivery_failure_reason(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution = executions.create_execution("delivery-failed", source="builtin")

    completed = executions.finish_execution(
        execution["id"],
        success=True,
        delivery_outcome="failed",
        delivery_error="Telegram send rejected",
    )

    assert completed["status"] == "completed"
    assert completed["error"] is None
    assert completed["delivery_outcome"] == "failed"
    assert completed["delivery_error"] == "Telegram send rejected"


def test_pending_delivery_is_finalized_idempotently_and_linked_to_execution(
    monkeypatch, tmp_path
):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution = executions.create_execution("morning-routine", source="builtin")

    pending = executions.record_execution_delivery(
        execution["id"],
        platform="telegram",
        chat_id=-100123,
        thread_id=77,
        status="pending",
    )
    assert executions.is_valid_delivery_token(pending["id"])
    assert pending["message_id"] is None
    assert pending["status"] == "pending"

    delivered = executions.record_execution_delivery(
        execution["id"],
        delivery_id=pending["id"],
        platform="telegram",
        chat_id=-100123,
        thread_id=77,
        message_id=456,
        status="delivered",
    )
    repeated = executions.record_execution_delivery(
        execution["id"],
        delivery_id=pending["id"],
        platform="telegram",
        chat_id=-100123,
        thread_id=77,
        message_id=456,
        status="delivered",
    )

    assert repeated == delivered
    assert delivered["execution_id"] == execution["id"]
    assert delivered["chat_id"] == "-100123"
    assert delivered["thread_id"] == "77"
    assert delivered["message_id"] == "456"
    assert executions.lookup_execution_delivery(
        pending["id"],
        platform="telegram",
        chat_id=-100123,
        message_id=456,
    ) == delivered
    assert executions.lookup_execution_delivery("../not-a-token") is None


def test_feedback_requires_exact_telegram_callback_coordinates_and_upserts(
    monkeypatch, tmp_path
):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution = executions.create_execution("feedback-job", source="builtin")
    delivery = executions.record_execution_delivery(
        execution["id"],
        platform="telegram",
        chat_id="-10055",
        thread_id="12",
        message_id="9001",
        status="delivered",
    )

    mismatches = (
        {"chat_id": "wrong", "message_id": "9001", "thread_id": "12"},
        {"chat_id": "-10055", "message_id": "wrong", "thread_id": "12"},
        {"chat_id": "-10055", "message_id": "9001", "thread_id": "wrong"},
    )
    for coordinates in mismatches:
        assert executions.record_execution_feedback(
            delivery["id"],
            vote=1,
            telegram_user_id=42,
            **coordinates,
        ) is None

    first = executions.record_execution_feedback(
        delivery["id"],
        vote=-1,
        telegram_user_id=42,
        chat_id="-10055",
        message_id="9001",
        thread_id="12",
        reason="prea lung",
    )
    changed = executions.record_execution_feedback(
        delivery["id"],
        vote=1,
        telegram_user_id=42,
        chat_id="-10055",
        message_id="9001",
        thread_id="12",
    )

    assert first["vote"] == -1
    assert changed["id"] == first["id"]
    assert changed["vote"] == 1
    assert changed["reason"] is None
    assert executions.list_execution_feedback(delivery["id"]) == [changed]

    with pytest.raises(ValueError, match="vote"):
        executions.record_execution_feedback(
            delivery["id"],
            vote=0,
            telegram_user_id=42,
            chat_id="-10055",
            message_id="9001",
            thread_id="12",
        )


def test_callback_can_confirm_a_pending_telegram_delivery(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution = executions.create_execution("timed-out-send", source="builtin")
    pending = executions.record_execution_delivery(
        execution["id"],
        platform="telegram",
        chat_id="123",
        status="pending",
    )

    feedback = executions.record_execution_feedback(
        pending["id"],
        vote=1,
        telegram_user_id="42",
        chat_id="123",
        thread_id="9",
        message_id="700",
    )

    assert feedback["vote"] == 1
    confirmed = executions.lookup_execution_delivery(pending["id"])
    assert confirmed["status"] == "delivered"
    assert confirmed["message_id"] == "700"
    assert executions.record_execution_feedback(
        pending["id"],
        vote=-1,
        telegram_user_id="77",
        chat_id="123",
        thread_id="9",
        message_id="wrong",
    ) is None

    failed = executions.record_execution_delivery(
        execution["id"],
        platform="telegram",
        chat_id="123",
        thread_id="9",
        status="pending",
    )
    executions.record_execution_delivery(
        execution["id"],
        delivery_id=failed["id"],
        platform="telegram",
        chat_id="123",
        thread_id="9",
        status="failed",
        error="send rejected",
    )
    assert executions.record_execution_feedback(
        failed["id"],
        vote=1,
        telegram_user_id="42",
        chat_id="123",
        thread_id="9",
        message_id="701",
    ) is None


def test_pending_delivery_cannot_be_relinked_to_another_thread(
    monkeypatch, tmp_path
):
    executions = _point_ledger(monkeypatch, tmp_path)
    execution = executions.create_execution("thread-bound", source="builtin")
    pending = executions.record_execution_delivery(
        execution["id"],
        platform="telegram",
        chat_id="123",
        thread_id="12",
        status="pending",
    )

    assert executions.record_execution_feedback(
        pending["id"],
        vote=1,
        telegram_user_id="42",
        chat_id="123",
        thread_id="99",
        message_id="700",
    ) is None
    assert executions.lookup_execution_delivery(pending["id"])["status"] == (
        "pending"
    )

    saved = executions.record_execution_feedback(
        pending["id"],
        vote=1,
        telegram_user_id="42",
        chat_id="123",
        thread_id="12",
        message_id="700",
    )
    assert saved["vote"] == 1


def test_delivery_message_cannot_be_relinked(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    first_execution = executions.create_execution("first", source="builtin")
    second_execution = executions.create_execution("second", source="builtin")
    delivery = executions.record_execution_delivery(
        first_execution["id"],
        platform="telegram",
        chat_id="123",
        message_id="7",
        status="delivered",
    )

    with pytest.raises(ValueError, match="different execution"):
        executions.record_execution_delivery(
            second_execution["id"],
            delivery_id=delivery["id"],
            platform="telegram",
            chat_id="123",
            message_id="7",
            status="delivered",
        )
    with pytest.raises(ValueError, match="message_id"):
        executions.record_execution_delivery(
            first_execution["id"],
            delivery_id=delivery["id"],
            platform="telegram",
            chat_id="123",
            message_id="8",
            status="delivered",
        )


def test_retention_preserves_voted_executions(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)

    voted = executions.create_execution("voted", source="builtin")
    delivery = executions.record_execution_delivery(
        voted["id"],
        platform="telegram",
        chat_id="123",
        message_id="1",
        status="delivered",
    )
    executions.record_execution_feedback(
        delivery["id"],
        vote=1,
        telegram_user_id="42",
        chat_id="123",
        message_id="1",
    )
    executions.finish_execution(voted["id"], success=True)

    for job_id in ("unvoted-old", "unvoted-new"):
        execution = executions.create_execution(job_id, source="builtin")
        executions.finish_execution(execution["id"], success=True)

    remaining = executions.list_executions(limit=100)
    assert {row["job_id"] for row in remaining} == {"voted", "unvoted-new"}
    assert executions.list_execution_feedback(delivery["id"])[0]["vote"] == 1


def test_database_triggers_preserve_feedback_across_binary_rollback(
    monkeypatch, tmp_path
):
    executions = _point_ledger(monkeypatch, tmp_path)
    voted = executions.create_execution("voted", source="builtin")
    voted_delivery = executions.record_execution_delivery(
        voted["id"],
        platform="telegram",
        chat_id="123",
        message_id="1",
        status="delivered",
    )
    executions.record_execution_feedback(
        voted_delivery["id"],
        vote=1,
        telegram_user_id="42",
        chat_id="123",
        message_id="1",
    )
    unvoted = executions.create_execution("unvoted", source="builtin")
    unvoted_delivery = executions.record_execution_delivery(
        unvoted["id"],
        platform="telegram",
        chat_id="123",
        message_id="2",
        status="delivered",
    )

    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        conn.execute(
            "DELETE FROM executions WHERE id IN (?, ?)",
            (voted["id"], unvoted["id"]),
        )

        assert conn.execute(
            "SELECT 1 FROM executions WHERE id=?", (voted["id"],)
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM execution_feedback WHERE delivery_id=?",
            (voted_delivery["id"],),
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM execution_deliveries WHERE id=?",
            (unvoted_delivery["id"],),
        ).fetchone() is None


def test_existing_execution_database_is_migrated_without_losing_rows(
    monkeypatch, tmp_path
):
    executions = _point_ledger(monkeypatch, tmp_path)
    database = executions.EXECUTIONS_FILE
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.execute(
            """CREATE TABLE executions (
                 id TEXT PRIMARY KEY,
                 job_id TEXT NOT NULL,
                 source TEXT NOT NULL,
                 process_id TEXT NOT NULL,
                 pid INTEGER NOT NULL,
                 process_started_at INTEGER,
                 status TEXT NOT NULL,
                 claimed_at TEXT NOT NULL,
                 started_at TEXT,
                 finished_at TEXT,
                 error TEXT
               )"""
        )
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, status, claimed_at)
               VALUES ('legacy-execution', 'legacy-job', 'builtin', 'owner', 1,
                       'claimed', '2026-09-01T08:00:00+00:00')"""
        )

    legacy = executions.update_execution_context(
        "legacy-execution",
        job_name="Legacy job",
        definition_hash="sha256:legacy",
    )
    delivery = executions.record_execution_delivery(
        "legacy-execution",
        platform="telegram",
        chat_id="100",
        message_id="200",
        status="delivered",
    )
    completed = executions.finish_execution(
        "legacy-execution", success=True, delivery_outcome="delivered"
    )

    assert legacy["job_name"] == "Legacy job"
    assert legacy["definition_hash"] == "sha256:legacy"
    assert completed["delivery_outcome"] == "delivered"
    assert completed["duration_ms"] is not None
    assert delivery["execution_id"] == "legacy-execution"
    assert executions.list_executions(job_id="legacy-job")[0]["id"] == (
        "legacy-execution"
    )
