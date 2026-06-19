from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .db import connect, init_db


@dataclass(frozen=True, slots=True)
class UsageStats:
    audio_cost_usd: float
    audio_duration_seconds: float
    audio_generated_count: int
    audio_input_text_tokens: int
    audio_output_audio_tokens: int
    audio_input_cost_usd: float
    audio_output_cost_usd: float
    audio_billed_cost_usd: float
    audio_billed_count: int
    summary_cost_usd: float
    reports_listened_count: int


def read_usage_stats(db_path: str | Path) -> UsageStats:
    conn = connect(db_path)
    try:
        return fetch_usage_stats(conn)
    finally:
        conn.close()


def fetch_usage_stats(conn: sqlite3.Connection) -> UsageStats:
    init_db(conn)
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(audio_cost_usd), 0) AS audio_cost_usd,
            COALESCE(SUM(audio_duration_seconds), 0) AS audio_duration_seconds,
            COALESCE(SUM(CASE WHEN audio_generated THEN 1 ELSE 0 END), 0) AS audio_generated_count,
            COALESCE(SUM(audio_input_text_tokens), 0) AS audio_input_text_tokens,
            COALESCE(SUM(audio_output_audio_tokens), 0) AS audio_output_audio_tokens,
            COALESCE(SUM(audio_input_cost_usd), 0) AS audio_input_cost_usd,
            COALESCE(SUM(audio_output_cost_usd), 0) AS audio_output_cost_usd,
            COALESCE(SUM(audio_billed_cost_usd), 0) AS audio_billed_cost_usd,
            COALESCE(SUM(CASE WHEN audio_billed_cost_usd IS NOT NULL THEN 1 ELSE 0 END), 0) AS audio_billed_count,
            COALESCE(SUM(summary_cost_usd), 0) AS summary_cost_usd,
            COALESCE(SUM(CASE WHEN spoken THEN 1 ELSE 0 END), 0) AS reports_listened_count
        FROM notifications
        """
    ).fetchone()
    return UsageStats(
        audio_cost_usd=float(_row_value(row, "audio_cost_usd", 0.0)),
        audio_duration_seconds=float(_row_value(row, "audio_duration_seconds", 0.0)),
        audio_generated_count=int(_row_value(row, "audio_generated_count", 0)),
        audio_input_text_tokens=int(_row_value(row, "audio_input_text_tokens", 0)),
        audio_output_audio_tokens=int(_row_value(row, "audio_output_audio_tokens", 0)),
        audio_input_cost_usd=float(_row_value(row, "audio_input_cost_usd", 0.0)),
        audio_output_cost_usd=float(_row_value(row, "audio_output_cost_usd", 0.0)),
        audio_billed_cost_usd=float(_row_value(row, "audio_billed_cost_usd", 0.0)),
        audio_billed_count=int(_row_value(row, "audio_billed_count", 0)),
        summary_cost_usd=float(_row_value(row, "summary_cost_usd", 0.0)),
        reports_listened_count=int(_row_value(row, "reports_listened_count", 0)),
    )


def read_last_voice_channel(db_path: str | Path) -> str | None:
    """Return the channel of the most recent voice delivery (openai_tts or macos_say)."""
    conn = connect(db_path)
    try:
        init_db(conn)
        row = conn.execute(
            """
            SELECT channel FROM notifications
            WHERE channel IN ('openai_tts', 'macos_say')
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return row["channel"] if isinstance(row, sqlite3.Row) else row[0]
    finally:
        conn.close()


def format_usd(amount: float) -> str:
    if amount <= 0:
        return "$0.0000"
    if amount < 0.0001:
        return "<$0.0001"
    if amount < 1:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _row_value(row: sqlite3.Row | tuple[object, ...] | None, key: str, default: object) -> object:
    if row is None:
        return default
    if isinstance(row, sqlite3.Row):
        return row[key]
    keys = {
        "audio_cost_usd": 0,
        "audio_duration_seconds": 1,
        "audio_generated_count": 2,
        "audio_input_text_tokens": 3,
        "audio_output_audio_tokens": 4,
        "audio_input_cost_usd": 5,
        "audio_output_cost_usd": 6,
        "audio_billed_cost_usd": 7,
        "audio_billed_count": 8,
        "summary_cost_usd": 9,
        "reports_listened_count": 10,
    }
    return row[keys[key]]
