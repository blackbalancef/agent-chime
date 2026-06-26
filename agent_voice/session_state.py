from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from .config import cache_warm_minutes
from .intelligence.fallback import build_single_message
from .models import (
    EventType,
    NormalizedEvent,
    NotificationCategory,
    SessionStatus,
    now_ts,
    stable_hash,
)


@dataclass(frozen=True, slots=True)
class NotificationCandidate:
    event_key: str
    session_id: str
    run_id: str
    agent_name: str
    project_name: str
    status: SessionStatus
    category: NotificationCategory
    priority: int
    message: str
    notification_hash: str
    created_at: int
    summary_source_text: str | None = None


def status_for_event(event: NormalizedEvent) -> SessionStatus:
    if event.event_type in {EventType.TASK_FAILED, EventType.TOOL_FAILED}:
        return SessionStatus.FAILED
    if event.event_type == EventType.PERMISSION_NEEDED:
        return SessionStatus.PERMISSION_NEEDED
    if event.event_type in {EventType.INPUT_NEEDED, EventType.SESSION_IDLE}:
        return SessionStatus.ATTENTION_REQUIRED
    if event.event_type in {
        EventType.TASK_FINISHED,
        EventType.SUBAGENT_FINISHED,
        EventType.LONG_RUNNING_FINISHED,
    }:
        return SessionStatus.COMPLETED
    return SessionStatus.RUNNING


def category_for_status(status: SessionStatus) -> NotificationCategory | None:
    if status == SessionStatus.FAILED:
        return NotificationCategory.FAILED
    if status in {SessionStatus.ATTENTION_REQUIRED, SessionStatus.PERMISSION_NEEDED}:
        return NotificationCategory.NEEDS_ATTENTION
    if status == SessionStatus.COMPLETED:
        return NotificationCategory.COMPLETED
    return None


def notification_hash_for(event: NormalizedEvent, status: SessionStatus) -> str:
    reason = event.ask_summary or event.attention_reason
    payload: dict[str, str | None] = {
        "session_id": event.session_id,
        "run_id": event.run_id,
        "status": status.value,
        "project_name": event.project_name,
    }
    if status in {SessionStatus.ATTENTION_REQUIRED, SessionStatus.PERMISSION_NEEDED, SessionStatus.FAILED}:
        payload["reason"] = reason
    if status == SessionStatus.COMPLETED:
        payload["event_key"] = event.event_key
        payload["summary"] = event.ask_summary
    return stable_hash(payload)


class SessionStateManager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        duplicate_cooldown_seconds: int = 300,
        language: str = "en",
        message_templates: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.conn = conn
        self.duplicate_cooldown_seconds = duplicate_cooldown_seconds
        self.language = language
        self.message_templates = message_templates or {}

    def apply_event(self, event: NormalizedEvent, *, now: int | None = None) -> NotificationCandidate | None:
        current_time = now or now_ts()
        new_status = status_for_event(event)
        category = category_for_status(new_status)
        session_id = event.session_id or "default"
        run_id = event.run_id or session_id
        existing = self.conn.execute(
            "SELECT * FROM session_states WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        if existing and not self._can_transition(existing, run_id, new_status):
            self._touch_existing(existing, event, current_time)
            return None

        notification_hash = notification_hash_for(event, new_status)
        should_notify = category is not None and self._should_notify(
            existing,
            run_id,
            new_status,
            notification_hash,
            current_time,
        )

        is_idle_reminder = (
            new_status == SessionStatus.ATTENTION_REQUIRED
            and event.event_type in {EventType.INPUT_NEEDED, EventType.SESSION_IDLE}
            and existing is not None
            and existing["last_notified_status"] == SessionStatus.COMPLETED.value
        )

        message = ""
        candidate: NotificationCandidate | None = None
        if should_notify and is_idle_reminder:
            message = self._build_reminder_message(event)
        elif should_notify:
            message = build_single_message(
                agent_name=event.agent_name,
                project_name=event.subject(),
                status=new_status,
                ask_summary=event.ask_summary,
                attention_reason=event.attention_reason,
                language=self.language,
                templates=self.message_templates.get(self.language),
            )
        if should_notify:
            candidate = NotificationCandidate(
                event_key=event.event_key,
                session_id=session_id,
                run_id=run_id,
                agent_name=event.agent_name,
                project_name=event.subject(),
                status=new_status,
                category=category,
                priority=event.priority or 100,
                message=message,
                notification_hash=notification_hash,
                created_at=current_time,
                summary_source_text=event.summary_source_text,
            )

        terminal_event_key = event.event_key if new_status in {SessionStatus.COMPLETED, SessionStatus.FAILED} else None
        metadata = {
            "attention_reason": event.attention_reason,
            "ask_summary": event.ask_summary,
            "last_event_key": event.event_key,
        }
        self.conn.execute(
            """
            INSERT INTO session_states (
                session_id,
                run_id,
                agent_name,
                project_name,
                cwd,
                status,
                last_notified_status,
                last_notification_hash,
                last_notified_at,
                last_event_at,
                terminal_event_key,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                run_id = excluded.run_id,
                agent_name = excluded.agent_name,
                project_name = excluded.project_name,
                cwd = excluded.cwd,
                status = excluded.status,
                last_notified_status = COALESCE(excluded.last_notified_status, session_states.last_notified_status),
                last_notification_hash = COALESCE(excluded.last_notification_hash, session_states.last_notification_hash),
                last_notified_at = COALESCE(excluded.last_notified_at, session_states.last_notified_at),
                last_event_at = excluded.last_event_at,
                terminal_event_key = COALESCE(excluded.terminal_event_key, session_states.terminal_event_key),
                metadata_json = excluded.metadata_json
            """,
            (
                session_id,
                run_id,
                event.agent_name,
                event.subject(),
                event.cwd,
                new_status.value,
                new_status.value if candidate else None,
                notification_hash if candidate else None,
                current_time if candidate else None,
                current_time,
                terminal_event_key,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        return candidate

    def _build_reminder_message(self, event: NormalizedEvent) -> str:
        templates = self.message_templates.get(self.language, {})
        template = templates.get("idle_reminder") or "{project} is waiting for your reply."
        agent_labels = {"claude-code": "Claude", "codex": "Codex", "pi": "Pi"}
        agent_label = agent_labels.get((event.agent_name or "").lower(), event.agent_name or "the agent")
        return template.format(
            project=event.subject(),
            minutes=cache_warm_minutes(event.agent_name),
            agent=agent_label,
        )

    def _can_transition(self, existing: sqlite3.Row, run_id: str, new_status: SessionStatus) -> bool:
        existing_run = existing["run_id"]
        existing_status = SessionStatus(existing["status"])
        if existing_run and existing_run != run_id:
            return True
        if existing_status == SessionStatus.FAILED and new_status != SessionStatus.FAILED:
            return False
        return True

    def _should_notify(
        self,
        existing: sqlite3.Row | None,
        run_id: str,
        new_status: SessionStatus,
        notification_hash: str,
        current_time: int,
    ) -> bool:
        if existing is None:
            return True

        existing_run = existing["run_id"]
        if existing_run and existing_run != run_id:
            return True

        last_hash = existing["last_notification_hash"]
        last_status = existing["last_notified_status"]
        last_notified_at = existing["last_notified_at"]
        if last_hash == notification_hash:
            return False
        if (
            last_status == new_status.value
            and new_status == SessionStatus.PERMISSION_NEEDED
            and isinstance(last_notified_at, int)
            and current_time - last_notified_at < self.duplicate_cooldown_seconds
        ):
            return False
        if last_status != new_status.value:
            return True
        return True

    def _touch_existing(self, existing: sqlite3.Row, event: NormalizedEvent, current_time: int) -> None:
        metadata = json.loads(existing["metadata_json"] or "{}")
        metadata["last_suppressed_event_key"] = event.event_key
        self.conn.execute(
            """
            UPDATE session_states
            SET last_event_at = ?, metadata_json = ?
            WHERE session_id = ?
            """,
            (
                current_time,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                existing["session_id"],
            ),
        )
