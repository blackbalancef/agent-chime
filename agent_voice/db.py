from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import NormalizedEvent, parse_event_type
from .tts_cost import DEFAULT_TTS_AUDIO_TOKENS_PER_SECOND

LEGACY_TTS_COST_PER_MINUTE_USD = 0.015


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT UNIQUE NOT NULL,
    agent_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    project_name TEXT,
    cwd TEXT,
    session_id TEXT,
    run_id TEXT,
    transcript_path TEXT,
    raw_payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 100,
    created_at INTEGER NOT NULL,
    processed_at INTEGER,
    attention_reason TEXT,
    ask_summary TEXT,
    summary_source_text TEXT,
    terminal_state TEXT,
    supersedes_event_key TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_pending
    ON events(status, priority, created_at);

CREATE TABLE IF NOT EXISTS session_states (
    session_id TEXT PRIMARY KEY,
    run_id TEXT,
    agent_name TEXT NOT NULL,
    project_name TEXT,
    cwd TEXT,
    status TEXT NOT NULL,
    last_notified_status TEXT,
    last_notification_hash TEXT,
    last_notified_at INTEGER,
    last_event_at INTEGER NOT NULL,
    terminal_event_key TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ids_json TEXT NOT NULL,
    category TEXT NOT NULL,
    channel TEXT NOT NULL,
    message TEXT NOT NULL,
    notification_hash TEXT,
    spoken BOOLEAN NOT NULL DEFAULT 0,
    audio_generated BOOLEAN NOT NULL DEFAULT 0,
    audio_duration_seconds REAL NOT NULL DEFAULT 0,
    audio_cost_usd REAL NOT NULL DEFAULT 0,
    audio_request_id TEXT,
    audio_client_request_id TEXT,
    audio_input_text_tokens INTEGER NOT NULL DEFAULT 0,
    audio_output_audio_tokens INTEGER NOT NULL DEFAULT 0,
    audio_input_cost_usd REAL NOT NULL DEFAULT 0,
    audio_output_cost_usd REAL NOT NULL DEFAULT 0,
    audio_billed_cost_usd REAL,
    audio_token_count_method TEXT,
    summary_cost_usd REAL NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    delivered_at INTEGER,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_created
    ON notifications(created_at);

CREATE TABLE IF NOT EXISTS pending_reminders (
    session_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    project_name TEXT,
    due_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_reminders_due
    ON pending_reminders(due_at);
"""

MIGRATED_COLUMNS: dict[str, tuple[str, ...]] = {
    "events": (
        "summary_source_text TEXT",
    ),
    "notifications": (
        "audio_generated BOOLEAN NOT NULL DEFAULT 0",
        "audio_duration_seconds REAL NOT NULL DEFAULT 0",
        "audio_cost_usd REAL NOT NULL DEFAULT 0",
        "audio_request_id TEXT",
        "audio_client_request_id TEXT",
        "audio_input_text_tokens INTEGER NOT NULL DEFAULT 0",
        "audio_output_audio_tokens INTEGER NOT NULL DEFAULT 0",
        "audio_input_cost_usd REAL NOT NULL DEFAULT 0",
        "audio_output_cost_usd REAL NOT NULL DEFAULT 0",
        "audio_billed_cost_usd REAL",
        "audio_token_count_method TEXT",
        "summary_cost_usd REAL NOT NULL DEFAULT 0",
    ),
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    ensure_migrated_columns(conn)
    backfill_legacy_audio_generated(conn)
    backfill_legacy_audio_metrics(conn)
    backfill_legacy_audio_cost_breakdown(conn)
    conn.commit()


def ensure_migrated_columns(conn: sqlite3.Connection) -> None:
    for table, definitions in MIGRATED_COLUMNS.items():
        existing = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for definition in definitions:
            name = definition.split(" ", 1)[0]
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
                existing.add(name)


def backfill_legacy_audio_generated(conn: sqlite3.Connection) -> None:
    count = conn.execute(
        """
        SELECT COUNT(*)
        FROM notifications
        WHERE channel = 'openai_tts'
          AND spoken = 1
          AND audio_generated = 0
        """
    ).fetchone()[0]
    if not count:
        return
    conn.execute(
        """
        UPDATE notifications
        SET audio_generated = 1
        WHERE channel = 'openai_tts'
          AND spoken = 1
          AND audio_generated = 0
        """
    )


def backfill_legacy_audio_metrics(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, message
        FROM notifications
        WHERE channel = 'openai_tts'
          AND spoken = 1
          AND audio_generated = 1
          AND audio_duration_seconds = 0
          AND audio_cost_usd = 0
        """
    ).fetchall()
    if not rows:
        return
    values = []
    for row in rows:
        row_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        message = row["message"] if isinstance(row, sqlite3.Row) else row[1]
        duration_seconds = estimate_audio_duration_seconds(str(message or ""))
        cost_usd = duration_seconds / 60 * LEGACY_TTS_COST_PER_MINUTE_USD
        output_audio_tokens = int(round(duration_seconds * DEFAULT_TTS_AUDIO_TOKENS_PER_SECOND))
        values.append((duration_seconds, cost_usd, output_audio_tokens, cost_usd, row_id))
    conn.executemany(
        """
        UPDATE notifications
        SET audio_duration_seconds = ?,
            audio_cost_usd = ?,
            audio_output_audio_tokens = ?,
            audio_output_cost_usd = ?,
            audio_token_count_method = 'legacy_per_minute'
        WHERE id = ?
        """,
        values,
    )


def backfill_legacy_audio_cost_breakdown(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, audio_duration_seconds, audio_cost_usd
        FROM notifications
        WHERE audio_generated = 1
          AND audio_cost_usd > 0
          AND audio_output_cost_usd = 0
        """
    ).fetchall()
    if not rows:
        return
    values = []
    for row in rows:
        row_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        duration_seconds = row["audio_duration_seconds"] if isinstance(row, sqlite3.Row) else row[1]
        cost_usd = row["audio_cost_usd"] if isinstance(row, sqlite3.Row) else row[2]
        output_audio_tokens = int(round(float(duration_seconds or 0) * DEFAULT_TTS_AUDIO_TOKENS_PER_SECOND))
        values.append((output_audio_tokens, float(cost_usd or 0), row_id))
    conn.executemany(
        """
        UPDATE notifications
        SET audio_output_audio_tokens = ?,
            audio_output_cost_usd = ?,
            audio_token_count_method = COALESCE(audio_token_count_method, 'legacy_per_minute')
        WHERE id = ?
        """,
        values,
    )


def estimate_audio_duration_seconds(message: str) -> float:
    text = " ".join(message.split())
    if not text:
        return 0.0
    word_count = len(text.split())
    return round(max(1.0, word_count / 2.7, len(text) / 14), 2)


def event_from_row(row: sqlite3.Row) -> NormalizedEvent:
    return NormalizedEvent(
        event_key=row["event_key"],
        agent_name=row["agent_name"],
        event_type=parse_event_type(row["event_type"]),
        project_name=row["project_name"],
        cwd=row["cwd"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        transcript_path=row["transcript_path"],
        raw_payload=json.loads(row["raw_payload_json"]),
        priority=row["priority"],
        created_at=row["created_at"],
        attention_reason=row["attention_reason"],
        ask_summary=row["ask_summary"],
        summary_source_text=row["summary_source_text"],
        terminal_state=row["terminal_state"],
        supersedes_event_key=row["supersedes_event_key"],
    )


def fetch_pending_events(conn: sqlite3.Connection, limit: int = 100) -> list[NormalizedEvent]:
    rows = conn.execute(
        """
        SELECT * FROM events
        WHERE status = 'pending'
        ORDER BY created_at ASC, id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [event_from_row(row) for row in rows]


def mark_events_processed(conn: sqlite3.Connection, event_keys: Iterable[str], processed_at: int) -> None:
    keys = list(event_keys)
    if not keys:
        return
    conn.executemany(
        """
        UPDATE events
        SET status = 'processed',
            processed_at = ?,
            summary_source_text = NULL
        WHERE event_key = ?
        """,
        [(processed_at, key) for key in keys],
    )


def count_prunable_events(
    conn: sqlite3.Connection, *, older_than_epoch: int, status: str = "processed"
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM events
        WHERE status = ? AND created_at < ?
        """,
        (status, older_than_epoch),
    ).fetchone()
    return int(row[0])


def prune_processed_events(conn: sqlite3.Connection, *, older_than_epoch: int) -> int:
    cursor = conn.execute(
        """
        DELETE FROM events
        WHERE status = 'processed' AND created_at < ?
        """,
        (older_than_epoch,),
    )
    conn.commit()
    return cursor.rowcount


def clear_events(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("DELETE FROM events")
    conn.commit()
    return cursor.rowcount


def clear_notifications(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("DELETE FROM notifications")
    conn.commit()
    return cursor.rowcount


def clear_session_states(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("DELETE FROM session_states")
    conn.commit()
    return cursor.rowcount


def schedule_reminder(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    agent_name: str,
    project_name: str | None,
    due_at: int,
    created_at: int,
) -> None:
    """Schedule (or reschedule) a timed idle reminder for ``session_id``.

    A later finish/idle for the same session overwrites ``due_at``, so only the
    most recent waiting state ever fires.
    """
    conn.execute(
        """
        INSERT INTO pending_reminders (session_id, agent_name, project_name, due_at, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            agent_name = excluded.agent_name,
            project_name = excluded.project_name,
            due_at = excluded.due_at,
            created_at = excluded.created_at
        """,
        (session_id, agent_name, project_name, int(due_at), int(created_at)),
    )


def cancel_reminder(conn: sqlite3.Connection, session_id: str) -> int:
    """Cancel any pending reminder for ``session_id`` (e.g. the user replied)."""
    cursor = conn.execute(
        "DELETE FROM pending_reminders WHERE session_id = ?", (session_id,)
    )
    conn.commit()
    return cursor.rowcount


def due_reminders(conn: sqlite3.Connection, now_epoch: int) -> list[sqlite3.Row]:
    """Return reminders whose ``due_at`` has arrived, oldest first."""
    return conn.execute(
        """
        SELECT session_id, agent_name, project_name, due_at, created_at
        FROM pending_reminders
        WHERE due_at <= ?
        ORDER BY due_at ASC
        """,
        (int(now_epoch),),
    ).fetchall()


def clear_reminders(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("DELETE FROM pending_reminders")
    conn.commit()
    return cursor.rowcount


def vacuum_db(conn: sqlite3.Connection) -> None:
    conn.commit()
    conn.execute("VACUUM")


def db_size_bytes(database_path: str | Path) -> int:
    path = Path(database_path).expanduser()
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0
