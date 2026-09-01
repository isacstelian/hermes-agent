"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue.
Interrupted attempts become ``unknown`` only after their exact owner process is
proved gone. Terminal states are immutable.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

# Optional test override. Production resolves the path at transaction time so
# dashboard operations that temporarily enter another profile cannot leak that
# profile's execution records into the import-time home.
EXECUTIONS_FILE: Optional[Path] = None
MAX_TERMINAL_EXECUTIONS = 1000
_TERMINAL_STATES = ("completed", "failed", "unknown")
_DELIVERY_STATES = ("pending", "delivered", "failed")
_DELIVERY_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex


def _connect() -> sqlite3.Connection:
    from cron.jobs import _ensure_cron_dir

    path = EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")
    _ensure_cron_dir(path.parent)
    return sqlite3.connect(path, timeout=5)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_wal_with_fallback(conn, db_label="cron/executions.db")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT,
             job_name TEXT,
             definition_hash TEXT,
             delivery_outcome TEXT,
             delivery_error TEXT,
             duration_ms INTEGER
           )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(executions)")}
    for name, declaration in (
        ("job_name", "TEXT"),
        ("definition_hash", "TEXT"),
        ("delivery_outcome", "TEXT"),
        ("delivery_error", "TEXT"),
        ("duration_ms", "INTEGER"),
    ):
        if name in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE executions ADD COLUMN {name} {declaration}")
        except sqlite3.OperationalError as exc:
            # Concurrent first-use connections may both observe the old schema.
            if "duplicate column" not in str(exc).lower():
                raise
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS execution_deliveries (
             id TEXT PRIMARY KEY,
             execution_id TEXT NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
             platform TEXT NOT NULL,
             chat_id TEXT NOT NULL,
             thread_id TEXT,
             message_id TEXT,
             status TEXT NOT NULL CHECK(status IN ('pending','delivered','failed')),
             error TEXT,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_deliveries_execution "
        "ON execution_deliveries(execution_id, created_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_deliveries_message "
        "ON execution_deliveries(platform, chat_id, message_id) "
        "WHERE message_id IS NOT NULL"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS execution_feedback (
             id TEXT PRIMARY KEY,
             execution_id TEXT NOT NULL
               REFERENCES executions(id) ON DELETE CASCADE,
             delivery_id TEXT NOT NULL
               REFERENCES execution_deliveries(id) ON DELETE CASCADE,
             telegram_user_id TEXT NOT NULL,
             vote INTEGER NOT NULL CHECK(vote IN (-1, 1)),
             reason TEXT,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL
           )"""
    )
    feedback_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(execution_feedback)")
    }
    if "execution_id" not in feedback_columns:
        try:
            conn.execute("ALTER TABLE execution_feedback ADD COLUMN execution_id TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.execute(
        """UPDATE execution_feedback
           SET execution_id=(
             SELECT d.execution_id FROM execution_deliveries d
             WHERE d.id=execution_feedback.delivery_id
           )
           WHERE execution_id IS NULL"""
    )
    # Older builds keyed votes by delivery. Collapse any duplicate votes from
    # one execution/user before adding the execution-level invariant, keeping
    # the most recently updated choice.
    conn.execute(
        """DELETE FROM execution_feedback
           WHERE execution_id IS NOT NULL
             AND EXISTS (
               SELECT 1 FROM execution_feedback newer
               WHERE newer.execution_id=execution_feedback.execution_id
                 AND newer.telegram_user_id=execution_feedback.telegram_user_id
                 AND (
                   newer.updated_at > execution_feedback.updated_at
                   OR (
                     newer.updated_at = execution_feedback.updated_at
                     AND newer.id > execution_feedback.id
                   )
                 )
             )"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_feedback_execution_user "
        "ON execution_feedback(execution_id, telegram_user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_feedback_delivery "
        "ON execution_feedback(delivery_id, updated_at DESC, id DESC)"
    )
    # These triggers remain in the database during a binary rollback. Older
    # Hermes versions do not enable foreign keys, so their retention delete
    # would otherwise orphan feedback or erase its execution linkage.
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS preserve_execution_feedback_history
           BEFORE DELETE ON executions
           WHEN EXISTS (
             SELECT 1 FROM execution_deliveries d
             JOIN execution_feedback f ON f.delivery_id=d.id
             WHERE d.execution_id=OLD.id
           )
           BEGIN
             SELECT RAISE(IGNORE);
           END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS cleanup_execution_deliveries
           AFTER DELETE ON executions
           BEGIN
             DELETE FROM execution_feedback
             WHERE delivery_id IN (
               SELECT id FROM execution_deliveries WHERE execution_id=OLD.id
             );
             DELETE FROM execution_deliveries WHERE execution_id=OLD.id;
           END"""
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, always close.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back
    the transaction; it does not close the connection. Relying on that alone
    leaks a connection (and its WAL/SHM file descriptors) on every call,
    since closing then depends on the garbage collector. Schema init runs
    inside the ``try`` too, so a PRAGMA/DDL failure after a successful
    ``connect()`` still closes the connection instead of leaking it.
    """
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _emit_execution_state(
    record: Optional[Dict[str, Any]], *, delivery_outcome: Optional[str] = None
) -> None:
    """Project durable state to monitoring without affecting ledger behavior."""
    try:
        from agent.monitoring.cron_health import emit_execution_state

        emit_execution_state(record, delivery_outcome=delivery_outcome)
    except Exception:
        pass


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    return current is not None and current == started_at


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    limit = max(0, int(MAX_TERMINAL_EXECUTIONS))
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT e.id FROM executions e
             WHERE e.status IN ('completed','failed','unknown')
               AND NOT EXISTS (
                 SELECT 1 FROM execution_deliveries d
                 JOIN execution_feedback f ON f.delivery_id=d.id
                 WHERE d.execution_id=e.id
               )
             ORDER BY e.claimed_at DESC, e.id DESC LIMIT -1 OFFSET ?
           )""",
        (limit,),
    )


def create_execution(
    job_id: str,
    *,
    source: str,
    job_name: Optional[str] = None,
    definition_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, job_name, definition_hash)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now,
             None if job_name is None else str(job_name),
             None if definition_hash is None else str(definition_hash)),
        )
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
    record = _record(row)
    _emit_execution_state(record)
    return record  # type: ignore[return-value]


def update_execution_context(
    execution_id: str,
    *,
    job_name: Optional[str] = None,
    definition_hash: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fill job metadata after a provider resolves the claimed definition.

    ``None`` leaves that field unchanged. The update is allowed only while the
    execution is claimed or running, matching the ledger's immutable-terminal
    contract.
    """
    assignments: List[str] = []
    params: List[Any] = []
    if job_name is not None:
        assignments.append("job_name=?")
        params.append(str(job_name))
    if definition_hash is not None:
        assignments.append("definition_hash=?")
        params.append(str(definition_hash))
    with _transaction() as conn:
        if assignments:
            params.append(str(execution_id))
            cur = conn.execute(
                "UPDATE executions SET " + ", ".join(assignments)
                + " WHERE id=? AND status IN ('claimed','running')",
                params,
            )
            if cur.rowcount != 1:
                return None
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?", (str(execution_id),)
        ).fetchone()
    return _record(row)


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status='running', started_at=?
               WHERE id=? AND status='claimed'""",
            (now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record)
    return record


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    delivery_outcome: Optional[str] = None,
    delivery_error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    finished = _hermes_now()
    now = finished.isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status=?, finished_at=?, error=?,
               delivery_outcome=?, delivery_error=?,
               duration_ms=MAX(0, CAST(ROUND(
                 (julianday(?) - julianday(COALESCE(started_at, claimed_at)))
                 * 86400000
               ) AS INTEGER))
               WHERE id=? AND status IN ('claimed','running')""",
            (status, now, detail,
             None if delivery_outcome is None else str(delivery_outcome),
             None if delivery_error is None else str(delivery_error),
             now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    return record


def is_valid_delivery_token(value: Any) -> bool:
    """Return whether ``value`` is a bounded callback-safe delivery token."""
    return isinstance(value, str) and _DELIVERY_TOKEN_RE.fullmatch(value) is not None


def _required_text(value: Any, field: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{field} must be non-empty")
    return text


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def record_execution_delivery(
    execution_id: str,
    *,
    platform: str,
    chat_id: Any,
    status: str,
    thread_id: Any = None,
    message_id: Any = None,
    error: Optional[str] = None,
    delivery_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or idempotently finalize one exact platform delivery.

    Create a ``pending`` row before sending so its random ``id`` can be used as
    a Telegram callback token. Pass that ``id`` back after the platform send
    with ``status='delivered'`` and the returned ``message_id``. Platform,
    execution, chat, and any already-recorded thread/message identity cannot be
    relinked.
    """
    execution_key = _required_text(execution_id, "execution_id")
    platform_key = _required_text(platform, "platform").lower()
    chat_key = _required_text(chat_id, "chat_id")
    thread_key = _optional_text(thread_id)
    message_key = _optional_text(message_id)
    state = _required_text(status, "status").lower()
    if state not in _DELIVERY_STATES:
        raise ValueError(
            "status must be one of: " + ", ".join(_DELIVERY_STATES)
        )
    token = delivery_id or uuid.uuid4().hex
    if not is_valid_delivery_token(token):
        raise ValueError("delivery_id must be a 32-character lowercase hex token")
    detail = _optional_text(error)
    now = _hermes_now().isoformat()

    with _transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM execution_deliveries WHERE id=?", (token,)
        ).fetchone()
        if existing is None:
            if conn.execute(
                "SELECT 1 FROM executions WHERE id=?", (execution_key,)
            ).fetchone() is None:
                raise ValueError("execution_id does not exist")
            if state == "delivered" and message_key is None:
                raise ValueError("message_id is required for a delivered delivery")
            try:
                conn.execute(
                    """INSERT INTO execution_deliveries
                       (id, execution_id, platform, chat_id, thread_id,
                        message_id, status, error, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (token, execution_key, platform_key, chat_key, thread_key,
                     message_key, state, detail, now, now),
                )
            except sqlite3.IntegrityError as exc:
                if "execution_deliveries.platform" in str(exc):
                    raise ValueError(
                        "platform/chat/message is already linked to another delivery"
                    ) from exc
                raise
        else:
            if existing["execution_id"] != execution_key:
                raise ValueError("delivery belongs to a different execution")
            if existing["platform"] != platform_key:
                raise ValueError("delivery platform cannot be changed")
            if existing["chat_id"] != chat_key:
                raise ValueError("delivery chat_id cannot be changed")
            if (
                existing["thread_id"] is not None
                and thread_key is not None
                and existing["thread_id"] != thread_key
            ):
                raise ValueError("delivery thread_id cannot be changed")
            if (
                existing["message_id"] is not None
                and message_key is not None
                and existing["message_id"] != message_key
            ):
                raise ValueError("delivery message_id cannot be changed")
            if existing["status"] == "delivered" and state != "delivered":
                raise ValueError("delivered delivery cannot return to a non-terminal state")
            if existing["status"] == "failed" and state == "pending":
                raise ValueError("failed delivery cannot return to pending")

            final_thread = existing["thread_id"] or thread_key
            final_message = existing["message_id"] or message_key
            if state == "delivered" and final_message is None:
                raise ValueError("message_id is required for a delivered delivery")
            unchanged = (
                existing["thread_id"] == final_thread
                and existing["message_id"] == final_message
                and existing["status"] == state
                and existing["error"] == detail
            )
            if unchanged:
                return dict(existing)
            try:
                conn.execute(
                    """UPDATE execution_deliveries
                       SET thread_id=?, message_id=?, status=?, error=?, updated_at=?
                       WHERE id=?""",
                    (final_thread, final_message, state, detail, now, token),
                )
            except sqlite3.IntegrityError as exc:
                if "execution_deliveries.platform" in str(exc):
                    raise ValueError(
                        "platform/chat/message is already linked to another delivery"
                    ) from exc
                raise
        row = conn.execute(
            "SELECT * FROM execution_deliveries WHERE id=?", (token,)
        ).fetchone()
    return dict(row)


def lookup_execution_delivery(
    delivery_token: Any,
    *,
    platform: Optional[str] = None,
    chat_id: Any = None,
    message_id: Any = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a callback-safe token, optionally requiring message coordinates."""
    if not is_valid_delivery_token(delivery_token):
        return None
    clauses = ["id=?"]
    params: List[Any] = [delivery_token]
    if platform is not None:
        clauses.append("platform=?")
        params.append(_required_text(platform, "platform").lower())
    if chat_id is not None:
        clauses.append("chat_id=?")
        params.append(_required_text(chat_id, "chat_id"))
    if message_id is not None:
        clauses.append("message_id=?")
        params.append(_required_text(message_id, "message_id"))
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM execution_deliveries WHERE " + " AND ".join(clauses),
            params,
        ).fetchone()
    return _record(row)


def _validated_feedback(
    *, telegram_user_id: Any, vote: int, reason: Optional[str]
) -> tuple[str, int, Optional[str]]:
    user_key = _required_text(telegram_user_id, "telegram_user_id")
    if isinstance(vote, bool) or not isinstance(vote, int) or vote not in (-1, 1):
        raise ValueError("vote must be either -1 or 1")
    return user_key, vote, _optional_text(reason)


def _upsert_feedback_unlocked(
    conn: sqlite3.Connection,
    execution_id: str,
    delivery_id: str,
    *,
    telegram_user_id: str,
    vote: int,
    reason: Optional[str],
) -> Dict[str, Any]:
    now = _hermes_now().isoformat()
    conn.execute(
        """INSERT INTO execution_feedback
           (id, execution_id, delivery_id, telegram_user_id, vote, reason,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(execution_id, telegram_user_id)
           DO UPDATE SET delivery_id=excluded.delivery_id,
                         vote=excluded.vote, reason=excluded.reason,
                         updated_at=excluded.updated_at""",
        (
            uuid.uuid4().hex,
            execution_id,
            delivery_id,
            telegram_user_id,
            vote,
            reason,
            now,
            now,
        ),
    )
    row = conn.execute(
        """SELECT * FROM execution_feedback
           WHERE execution_id=? AND telegram_user_id=?""",
        (execution_id, telegram_user_id),
    ).fetchone()
    return dict(row)


def upsert_execution_feedback(
    delivery_token: Any,
    *,
    telegram_user_id: Any,
    vote: int,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Upsert one Telegram user's vote for an already-delivered message."""
    if not is_valid_delivery_token(delivery_token):
        return None
    user_key, clean_vote, clean_reason = _validated_feedback(
        telegram_user_id=telegram_user_id, vote=vote, reason=reason
    )
    with _transaction() as conn:
        delivery = conn.execute(
            """SELECT id, execution_id FROM execution_deliveries
               WHERE id=? AND platform='telegram' AND status='delivered'
                 AND message_id IS NOT NULL""",
            (delivery_token,),
        ).fetchone()
        if delivery is None:
            return None
        return _upsert_feedback_unlocked(
            conn,
            delivery["execution_id"],
            delivery["id"],
            telegram_user_id=user_key,
            vote=clean_vote,
            reason=clean_reason,
        )


def record_execution_feedback(
    feedback_token: Any,
    *,
    vote: int,
    telegram_user_id: Any,
    chat_id: Any,
    message_id: Any,
    thread_id: Any = None,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist a Telegram callback only when token and message exactly match.

    Invalid, expired, or mismatched tokens return ``None``. A valid repeat vote
    from the same user updates the existing row, so users can change feedback.
    """
    if not is_valid_delivery_token(feedback_token):
        return None
    user_key, clean_vote, clean_reason = _validated_feedback(
        telegram_user_id=telegram_user_id, vote=vote, reason=reason
    )
    chat_key = _required_text(chat_id, "chat_id")
    message_key = _required_text(message_id, "message_id")
    thread_key = _optional_text(thread_id)
    with _transaction() as conn:
        delivery = conn.execute(
            """SELECT id, execution_id, status, message_id, thread_id
               FROM execution_deliveries
               WHERE id=? AND platform='telegram' AND chat_id=?""",
            (feedback_token, chat_key),
        ).fetchone()
        if delivery is None:
            return None
        if delivery["status"] == "failed":
            return None
        if (
            delivery["status"] == "pending"
            and delivery["thread_id"] is not None
            and delivery["thread_id"] != thread_key
        ):
            return None
        if (
            delivery["status"] == "delivered"
            and delivery["thread_id"] != thread_key
        ):
            return None
        if (
            delivery["message_id"] is not None
            and delivery["message_id"] != message_key
        ):
            return None
        if delivery["status"] == "pending":
            try:
                conn.execute(
                    """UPDATE execution_deliveries
                       SET status='delivered', thread_id=?, message_id=?,
                           error=NULL, updated_at=?
                       WHERE id=? AND status='pending'""",
                    (
                        thread_key,
                        message_key,
                        _hermes_now().isoformat(),
                        delivery["id"],
                    ),
                )
            except sqlite3.IntegrityError:
                # Another delivery already owns these Telegram coordinates.
                return None
        elif delivery["message_id"] != message_key:
            return None
        return _upsert_feedback_unlocked(
            conn,
            delivery["execution_id"],
            delivery["id"],
            telegram_user_id=user_key,
            vote=clean_vote,
            reason=clean_reason,
        )


def list_execution_feedback(delivery_token: Any) -> List[Dict[str, Any]]:
    """Return all current user votes for one callback-safe delivery token."""
    if not is_valid_delivery_token(delivery_token):
        return []
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM execution_feedback WHERE delivery_id=?
               ORDER BY updated_at DESC, id DESC""",
            (delivery_token,),
        ).fetchall()
    return [dict(row) for row in rows]


def routine_feedback_summary(
    *, job_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return one local execution/feedback aggregate per routine."""
    where = "WHERE e.job_id=?" if job_id is not None else ""
    params: List[Any] = [str(job_id)] if job_id is not None else []
    with _transaction() as conn:
        rows = conn.execute(
            f"""WITH execution_stats AS (
                   SELECT e.job_id,
                          (SELECT e2.job_name FROM executions e2
                           WHERE e2.job_id=e.job_id AND e2.job_name IS NOT NULL
                           ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)
                            AS job_name,
                          COUNT(*) AS runs,
                          SUM(CASE WHEN e.status='completed' THEN 1 ELSE 0 END)
                            AS completed_runs,
                          SUM(CASE WHEN e.status IN ('failed','unknown')
                                   THEN 1 ELSE 0 END) AS failed_runs,
                          COALESCE(SUM(e.duration_ms), 0) AS total_duration_ms,
                          CAST(ROUND(AVG(e.duration_ms)) AS INTEGER)
                            AS average_duration_ms,
                          MAX(e.claimed_at) AS last_run_at
                   FROM executions e
                   {where}
                   GROUP BY e.job_id
                 ),
                 delivery_stats AS (
                   SELECT e.job_id,
                          COUNT(*) AS deliveries,
                          SUM(CASE WHEN d.status='delivered' THEN 1 ELSE 0 END)
                            AS delivered_deliveries,
                          SUM(CASE WHEN d.status='failed' THEN 1 ELSE 0 END)
                            AS failed_deliveries,
                          SUM(CASE WHEN d.status='pending' THEN 1 ELSE 0 END)
                            AS pending_deliveries
                   FROM execution_deliveries d
                   JOIN executions e ON e.id=d.execution_id
                   {where}
                   GROUP BY e.job_id
                 ),
                 feedback_stats AS (
                   SELECT e.job_id,
                          COUNT(*) AS votes,
                          SUM(CASE WHEN f.vote=1 THEN 1 ELSE 0 END)
                            AS positive_votes,
                          SUM(CASE WHEN f.vote=-1 THEN 1 ELSE 0 END)
                            AS negative_votes,
                          COUNT(DISTINCT f.delivery_id) AS rated_deliveries
                   FROM execution_feedback f
                   JOIN executions e ON e.id=f.execution_id
                   {where}
                   GROUP BY e.job_id
                 )
                 SELECT x.*,
                        COALESCE(d.deliveries, 0) AS deliveries,
                        COALESCE(d.delivered_deliveries, 0)
                          AS delivered_deliveries,
                        COALESCE(d.failed_deliveries, 0)
                          AS failed_deliveries,
                        COALESCE(d.pending_deliveries, 0)
                          AS pending_deliveries,
                        COALESCE(f.votes, 0) AS votes,
                        COALESCE(f.positive_votes, 0) AS positive_votes,
                        COALESCE(f.negative_votes, 0) AS negative_votes,
                        COALESCE(f.rated_deliveries, 0) AS rated_deliveries
                 FROM execution_stats x
                 LEFT JOIN delivery_stats d ON d.job_id=x.job_id
                 LEFT JOIN feedback_stats f ON f.job_id=x.job_id
                 ORDER BY x.last_run_at DESC, x.job_id""",
            params + params + params,
        ).fetchall()
    return [dict(row) for row in rows]


def list_routine_feedback(
    *, job_id: Optional[str] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    """Return newest local feedback rows with their execution coordinates."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("e.job_id=?")
        params.append(str(job_id))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT f.*, e.job_id, e.job_name, e.claimed_at,
                      e.duration_ms, d.chat_id, d.thread_id, d.message_id
               FROM execution_feedback f
               JOIN executions e ON e.id=f.execution_id
               JOIN execution_deliveries d ON d.id=f.delivery_id"""
            + where
            + " ORDER BY f.updated_at DESC, f.id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries."""
    now = _hermes_now().isoformat()
    changed = 0
    recovered: List[Dict[str, Any]] = []
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, process_id, pid, process_started_at FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                continue
            cur = conn.execute(
                """UPDATE executions SET status='unknown', finished_at=?, error=?,
                   duration_ms=MAX(0, CAST(ROUND(
                     (julianday(?) - julianday(COALESCE(started_at, claimed_at)))
                     * 86400000
                   ) AS INTEGER))
                   WHERE id=? AND status IN ('claimed','running')""",
                (now,
                 "Scheduler restarted after this execution's owner exited before a durable "
                 "terminal state; whether side effects ran is unknown.",
                 now, row["id"]),
            )
            changed += cur.rowcount
            if cur.rowcount:
                record = _record(conn.execute(
                    "SELECT * FROM executions WHERE id=?", (row["id"],)
                ).fetchone())
                if record is not None:
                    recovered.append(record)
        if changed:
            _prune_unlocked(conn)
    for record in recovered:
        _emit_execution_state(record)
    return changed


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50,
    before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        clauses.append("claimed_at < ?")
        params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one indexed query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
