from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import AgentVoiceConfig, load_config
from .db import connect, fetch_pending_events, init_db, mark_events_processed, prune_processed_events, vacuum_db
from .delivery import DeliveryRouter
from .heartbeat import write_heartbeat
from .intelligence.fallback import build_grouped_message
from .intelligence.pipeline_log import log_summary_pipeline
from .intelligence.summarizer import summarize_notification
from .models import EventType, NotificationCategory, now_ts, stable_hash
from .runtime import (
    clear_active_voice_sessions,
    read_runtime_state,
    set_active_voice_sessions,
    write_runtime_state,
)
from .session_state import NotificationCandidate, SessionStateManager
from .usage import fetch_usage_stats, start_of_day_epoch, start_of_month_epoch

# How often the daemon runs a VACUUM to reclaim space from pruned rows.
VACUUM_INTERVAL_SECONDS = 24 * 60 * 60
# Voice channels that constitute a paid/spoken delivery for rate-limit accounting.
VOICE_CHANNELS = ("openai_tts", "macos_say")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    processed_events: int
    notifications_created: int
    notifications_delivered: int


def _agent_version() -> str:
    try:
        from . import __version__

        if __version__:
            return str(__version__)
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        from importlib import metadata

        return metadata.version("voiccce")
    except Exception:  # pragma: no cover - metadata absent in source checkouts
        return "unknown"


def _log(message: str) -> None:
    """Emit a timestamped daemon log line to stderr (captured into daemon.log)."""
    print(f"[voiccce daemon] {message}", file=sys.stderr, flush=True)


def in_quiet_hours(config: AgentVoiceConfig, *, now: float | None = None) -> bool:
    """Return whether the wall clock is currently inside the quiet-hours window.

    The window is an ``HH:MM``–``HH:MM`` range in the configured timezone and may
    wrap past midnight (e.g. ``23:00``–``09:00``). Returns ``False`` when quiet
    hours are disabled. ``now`` is wall-clock epoch seconds (defaults to the real
    current time) — independent of the event-processing clock.
    """
    if not config.quiet_hours_enabled:
        return False
    try:
        tz = ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        tz = datetime.now().astimezone().tzinfo
    current = datetime.fromtimestamp(time.time() if now is None else now, tz)
    minutes = current.hour * 60 + current.minute
    start = _hhmm_to_minutes(config.quiet_hours_from)
    end = _hhmm_to_minutes(config.quiet_hours_to)
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    # Window wraps past midnight: inside if at/after start OR before end.
    return minutes >= start or minutes < end


def _hhmm_to_minutes(value: str) -> int:
    hour, _, minute = value.partition(":")
    try:
        return int(hour) * 60 + int(minute)
    except ValueError:
        return 0


def run_maintenance(
    conn: sqlite3.Connection,
    config: AgentVoiceConfig,
    *,
    current_time: int,
    now: float | None = None,
) -> int:
    """Prune expired processed events and VACUUM periodically.

    Returns the number of events pruned this cycle. Retention uses the event clock
    (``current_time``) so the cutoff matches stored ``created_at`` epochs; the
    VACUUM cadence uses the wall clock (``now``) tracked in the runtime state, so a
    daemon that just started establishes a baseline instead of vacuuming on its
    first cycle. Errors are swallowed so maintenance never crashes the daemon.
    """
    pruned = 0
    if config.event_retention_days > 0:
        cutoff = current_time - config.event_retention_days * 86400
        try:
            pruned = prune_processed_events(conn, older_than_epoch=cutoff)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            _log(f"event prune failed: {exc}")

    wall_now = time.time() if now is None else now
    try:
        state = read_runtime_state(config)
        last_vacuum = state.get("last_vacuum_at")
        if not isinstance(last_vacuum, (int, float)):
            # First sighting: baseline now without vacuuming.
            state["last_vacuum_at"] = wall_now
            write_runtime_state(config, state)
        elif wall_now - float(last_vacuum) >= VACUUM_INTERVAL_SECONDS:
            vacuum_db(conn)
            state["last_vacuum_at"] = wall_now
            write_runtime_state(config, state)
            _log("ran periodic VACUUM")
    except (OSError, sqlite3.Error) as exc:  # pragma: no cover - defensive
        _log(f"vacuum maintenance failed: {exc}")
    return pruned


def _spend_cap_reached(conn: sqlite3.Connection, config: AgentVoiceConfig) -> str | None:
    """Return ``"daily"``/``"monthly"`` when a configured spend cap is at/over budget.

    Spend is the total audio + summary cost recorded since the start of the current
    local day (or month) in the configured timezone. Caps of ``0`` mean "no cap".
    """
    if config.daily_spend_cap_usd > 0:
        since = start_of_day_epoch(config.timezone)
        stats = fetch_usage_stats(conn, since=since)
        spend = stats.audio_cost_usd + stats.summary_cost_usd
        if spend >= config.daily_spend_cap_usd:
            return "daily"
    if config.monthly_spend_cap_usd > 0:
        since = start_of_month_epoch(config.timezone)
        stats = fetch_usage_stats(conn, since=since)
        spend = stats.audio_cost_usd + stats.summary_cost_usd
        if spend >= config.monthly_spend_cap_usd:
            return "monthly"
    return None


def _recent_voice_delivery_count(conn: sqlite3.Connection, since: int) -> int:
    """Number of voice notifications spoken at/after ``since`` (rate-limit window)."""
    placeholders = ",".join("?" * len(VOICE_CHANNELS))
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM notifications
        WHERE channel IN ({placeholders})
          AND spoken = 1
          AND delivered_at IS NOT NULL
          AND delivered_at >= ?
        """,
        (*VOICE_CHANNELS, since),
    ).fetchone()
    return int(row[0] if not isinstance(row, sqlite3.Row) else row[0])


def process_once(
    conn: sqlite3.Connection,
    config: AgentVoiceConfig,
    *,
    deliver: bool = True,
    terminal_only: bool = False,
    current_time: int | None = None,
) -> ProcessResult:
    init_db(conn)
    events = fetch_pending_events(conn)
    if not events:
        return ProcessResult(processed_events=0, notifications_created=0, notifications_delivered=0)

    current_time = current_time or now_ts()
    manager = SessionStateManager(
        conn,
        duplicate_cooldown_seconds=config.duplicate_cooldown_seconds,
        language=config.language,
        message_templates=config.message_templates,
    )
    candidates_by_session: dict[str, NotificationCandidate] = {}
    processed_keys: list[str] = []

    for event in events:
        if not _event_type_enabled(config, event.event_type):
            processed_keys.append(event.event_key)
            continue
        candidate = manager.apply_event(event, now=current_time)
        if candidate:
            candidates_by_session[candidate.session_id] = candidate
        processed_keys.append(event.event_key)

    mark_events_processed(conn, processed_keys, current_time)

    notifications_created = 0
    notifications_delivered = 0
    if candidates_by_session:
        candidates = list(candidates_by_session.values())
        candidates.sort(key=lambda candidate: (candidate.priority, candidate.created_at))

        voice_allowed = config.voice_enabled
        desktop_allowed = config.desktop_enabled
        force_backend: str | None = None
        suppressed_reason: str | None = None
        if deliver and not terminal_only and voice_allowed and config.min_seconds_between_voice_messages > 0:
            since = current_time - config.min_seconds_between_voice_messages
            recent_sessions = _recently_voiced_sessions(conn, since)
            candidate_sessions = {candidate.session_id for candidate in candidates}
            # Throttle only same-session back-to-back voice. A different session is
            # always voiced — the serial daemon plays it right after the previous one
            # (no overlap), so distinct sessions are announced one after another.
            if candidate_sessions and candidate_sessions <= recent_sessions:
                voice_allowed = False

        # Quiet hours: silence voice and/or desktop in the configured window. This
        # is enforced at the daemon layer so DeliveryRouter's core semantics stay
        # intact (it never consults the clock).
        if deliver and not terminal_only and in_quiet_hours(config):
            if not config.quiet_hours_voice and voice_allowed:
                voice_allowed = False
                suppressed_reason = "quiet_hours"
            if not config.quiet_hours_desktop:
                desktop_allowed = False

        # Rate limit: count voice notifications spoken in the trailing 60s and, once
        # at/over the cap, suppress voice for this cycle (group it down to a silent
        # channel) so no paid TTS — or paid summary — request is made.
        if deliver and not terminal_only and voice_allowed and config.max_events_per_minute > 0:
            recent_voice = _recent_voice_delivery_count(conn, current_time - 60)
            if recent_voice >= config.max_events_per_minute:
                voice_allowed = False
                suppressed_reason = "rate_limited"

        # Spend cap: before any paid summary/TTS, total today's (and this month's)
        # spend and, when at/over a cap, suppress every metered call this cycle.
        # The cap is decoupled from the voice backend because the paid GPT summary
        # is metered regardless of backend — on the default macos_say backend a cap
        # must still skip the summary (and there is no paid TTS to force away from).
        # ``force_backend`` is only set when the configured backend is openai_tts,
        # so a custom/free backend keeps routing normally; ``capped`` always feeds
        # the summary gate below.
        capped: str | None = None
        if deliver and not terminal_only and voice_allowed:
            capped = _spend_cap_reached(conn, config)
            if capped is not None:
                if config.voice_backend == "openai_tts":
                    force_backend = "macos_say"
                _log(
                    f"{capped} spend cap reached — skipping paid summary"
                    + (
                        " and falling back to free macos_say for this cycle"
                        if config.voice_backend == "openai_tts"
                        else " for this cycle"
                    )
                )

        summary_cost_usd = 0.0
        summary_result = None
        primary = candidates[0]
        # Honor the privacy choice: only persist the full last assistant message to the
        # pipeline log when the user opted into full_last_message. Otherwise log the
        # already-short notification text, mirroring summarizer._source_text.
        if config.summary_privacy_level == "full_last_message":
            source_text = primary.summary_source_text or primary.message
        else:
            source_text = primary.message
        # Skip the paid GPT summary when a spend cap is reached — the summary is
        # itself a metered call we must not make over the cap, regardless of the
        # configured voice backend (the default macos_say still bills summaries).
        if (
            deliver
            and not terminal_only
            and voice_allowed
            and capped is None
            and len(candidates) == 1
        ):
            summary_result = summarize_notification(config, primary)
            summary_cost_usd = summary_result.cost_usd
            if summary_result.message:
                candidates[0] = replace(candidates[0], message=summary_result.message)
        message = build_grouped_message(
            candidates,
            language=config.language,
            templates=config.message_templates.get(config.language),
        )
        category = (
            NotificationCategory.GROUPED_SUMMARY
            if len(candidates) > 1
            else candidates[0].category
        )
        notification_hash = stable_hash([candidate.notification_hash for candidate in candidates])
        event_ids = [candidate.event_key for candidate in candidates]
        channel = "none"
        spoken = False
        audio_generated = False
        audio_duration_seconds = 0.0
        audio_cost_usd = 0.0
        audio_request_id = None
        audio_client_request_id = None
        audio_input_text_tokens = 0
        audio_output_audio_tokens = 0
        audio_input_cost_usd = 0.0
        audio_output_cost_usd = 0.0
        audio_billed_cost_usd = None
        audio_token_count_method = None
        delivered_at = None
        error = None

        if deliver:
            overrides: dict[str, object] = {}
            if not voice_allowed:
                overrides["voice_enabled"] = False
            if not desktop_allowed:
                overrides["desktop_enabled"] = False
            delivery_config = replace(config, **overrides) if overrides else config
            router_kwargs: dict[str, object] = {"terminal_only": terminal_only}
            if force_backend is not None:
                # Only pass the (additive) override when a spend cap forced it, so
                # custom/fake routers without the parameter keep working.
                router_kwargs["force_backend"] = force_backend
            router = DeliveryRouter(delivery_config, **router_kwargs)
            voicing = voice_allowed and delivery_config.voice_enabled and not terminal_only
            if voicing:
                # Record which sessions are being spoken so a UserPromptSubmit hook
                # for the same session can cut the playback short.
                set_active_voice_sessions(
                    config,
                    [candidate.session_id for candidate in candidates],
                    now=current_time,
                )
            try:
                results = router.deliver(message)
            finally:
                if voicing:
                    clear_active_voice_sessions(config)
            audio_generated = any(result.audio_generated for result in results)
            audio_duration_seconds = sum(result.audio_duration_seconds for result in results)
            audio_cost_usd = sum(result.audio_cost_usd for result in results)
            request_ids = [result.audio_request_id for result in results if result.audio_request_id]
            client_request_ids = [
                result.audio_client_request_id for result in results if result.audio_client_request_id
            ]
            token_methods = [
                result.audio_token_count_method for result in results if result.audio_token_count_method
            ]
            audio_request_id = ",".join(request_ids) if request_ids else None
            audio_client_request_id = ",".join(client_request_ids) if client_request_ids else None
            audio_input_text_tokens = sum(result.audio_input_text_tokens for result in results)
            audio_output_audio_tokens = sum(result.audio_output_audio_tokens for result in results)
            audio_input_cost_usd = sum(result.audio_input_cost_usd for result in results)
            audio_output_cost_usd = sum(result.audio_output_cost_usd for result in results)
            audio_token_count_method = ",".join(dict.fromkeys(token_methods)) if token_methods else None
            successful = next((result for result in results if result.delivered), None)
            if successful:
                channel = successful.channel
                spoken = successful.spoken
                delivered_at = current_time
                notifications_delivered = 1
                _log(
                    f"delivered via {channel} (cost ${audio_cost_usd:.4f}): {message}"
                )
            elif results:
                channel = results[-1].channel
                error = "; ".join(result.error or "delivery failed" for result in results if not result.delivered)
                _log(f"delivery failed via {channel}: {error}")
            # No channel produced a delivery at all (e.g. every channel suppressed):
            # record why voice was withheld so the audit trail is explicit.
            if suppressed_reason and channel in {"none", ""}:
                channel = suppressed_reason
            elif suppressed_reason and not spoken:
                error = "; ".join(filter(None, [error, f"voice suppressed: {suppressed_reason}"]))

        conn.execute(
            """
            INSERT INTO notifications (
                event_ids_json,
                category,
                channel,
                message,
                notification_hash,
                spoken,
                audio_generated,
                audio_duration_seconds,
                audio_cost_usd,
                audio_request_id,
                audio_client_request_id,
                audio_input_text_tokens,
                audio_output_audio_tokens,
                audio_input_cost_usd,
                audio_output_cost_usd,
                audio_billed_cost_usd,
                audio_token_count_method,
                summary_cost_usd,
                created_at,
                delivered_at,
                error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                json.dumps(event_ids, ensure_ascii=False),
                category.value,
                channel,
                message,
                notification_hash,
                int(spoken),
                int(audio_generated),
                audio_duration_seconds,
                audio_cost_usd,
                audio_request_id,
                audio_client_request_id,
                audio_input_text_tokens,
                audio_output_audio_tokens,
                audio_input_cost_usd,
                audio_output_cost_usd,
                audio_billed_cost_usd,
                audio_token_count_method,
                summary_cost_usd,
                current_time,
                delivered_at,
                error,
            ),
        )
        notifications_created = 1

        log_summary_pipeline(
            config,
            {
                "ts": current_time,
                "project": primary.project_name,
                "status": getattr(primary.status, "value", str(primary.status)),
                "grouped": len(candidates) > 1,
                "gpt_enabled": bool(config.summary_enabled and config.summary_provider == "openai"),
                "gpt_used": bool(summary_result and summary_result.message),
                "gpt_error": summary_result.error if summary_result else None,
                "source_text": source_text,
                "prompt": summary_result.prompt if summary_result else None,
                "gpt_raw_output": summary_result.raw_text if summary_result else None,
                "gpt_clean_output": summary_result.message if summary_result else None,
                "spoken_text": message,
                "channel": channel,
                "spoken": bool(spoken),
                "summary_cost_usd": summary_cost_usd,
                "delivery_error": error,
            },
        )

    conn.commit()
    return ProcessResult(
        processed_events=len(processed_keys),
        notifications_created=notifications_created,
        notifications_delivered=notifications_delivered,
    )


def _event_type_enabled(config: AgentVoiceConfig, event_type: EventType) -> bool:
    if event_type in {EventType.TASK_FINISHED, EventType.LONG_RUNNING_FINISHED}:
        return config.notify_task_finished
    if event_type == EventType.SUBAGENT_FINISHED:
        return config.notify_subagent_finished
    if event_type == EventType.PERMISSION_NEEDED:
        return config.notify_permission_needed
    if event_type in {EventType.INPUT_NEEDED, EventType.SESSION_IDLE}:
        return config.notify_input_needed
    if event_type in {EventType.TASK_FAILED, EventType.TOOL_FAILED}:
        return config.notify_task_failed
    return True


def _recently_voiced_sessions(conn: sqlite3.Connection, since: int) -> set[str]:
    """Return session ids that were spoken aloud at or after ``since``."""
    rows = conn.execute(
        """
        SELECT event_ids_json FROM notifications
        WHERE channel IN ('openai_tts', 'macos_say')
          AND spoken = 1
          AND delivered_at IS NOT NULL
          AND delivered_at >= ?
        """,
        (since,),
    ).fetchall()
    event_keys: set[str] = set()
    for row in rows:
        value = row["event_ids_json"] if isinstance(row, sqlite3.Row) else row[0]
        try:
            event_keys.update(json.loads(value or "[]"))
        except (TypeError, ValueError):
            continue
    if not event_keys:
        return set()
    placeholders = ",".join("?" * len(event_keys))
    session_rows = conn.execute(
        f"SELECT DISTINCT session_id FROM events WHERE event_key IN ({placeholders})",
        tuple(event_keys),
    ).fetchall()
    sessions: set[str] = set()
    for row in session_rows:
        value = row["session_id"] if isinstance(row, sqlite3.Row) else row[0]
        if value:
            sessions.add(value)
    return sessions


def _config_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def maybe_reload_config(
    config: AgentVoiceConfig, config_path: Path, last_mtime: float | None
) -> tuple[AgentVoiceConfig, float | None]:
    """Reload config from disk when the file changed; otherwise return it unchanged.

    Returns the (possibly new) config and the mtime to track next. On a transient
    read/parse error (e.g. a non-atomic write caught mid-flight) the previous config
    and mtime are kept so the next poll cycle retries.
    """
    current_mtime = _config_mtime(config_path)
    if current_mtime is None or current_mtime == last_mtime:
        return config, last_mtime
    try:
        return load_config(config_path), current_mtime
    except Exception as exc:  # partial write / transient parse error — retry next cycle
        _log(f"config reload failed: {exc}")
        return config, last_mtime


def _startup_banner(config: AgentVoiceConfig) -> str:
    quiet = (
        f"quiet_hours={config.quiet_hours_from}-{config.quiet_hours_to} "
        f"({'enabled' if config.quiet_hours_enabled else 'disabled'})"
    )
    return (
        f"started v{_agent_version()} pid={os.getpid()} "
        f"voice_backend={config.voice_backend} "
        f"poll_interval={config.poll_interval_ms}ms "
        f"{quiet} "
        f"db={config.database_path}"
    )


def _write_daemon_pid(config: AgentVoiceConfig) -> Path | None:
    """Record this process's pid in the daemon pid file (best-effort).

    Even a launchd-managed daemon — started without going through
    :func:`service.start_daemon` — writes its own pid file here so
    ``daemon_status``/``stop``/``doctor`` can see and signal it. Returns the pid
    path on success so a clean exit can clear it.
    """
    from .service import service_paths

    pid_path = service_paths(config).pid_path
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        _log(f"could not write pid file {pid_path}: {exc}")
        return None
    return pid_path


def run_daemon(config: AgentVoiceConfig, *, once: bool = False, deliver: bool = True, terminal_only: bool = False) -> None:
    conn = connect(config.database_path)
    init_db(conn)
    config_path = config.config_path
    last_mtime = _config_mtime(config_path)
    _log(_startup_banner(config))
    # Claim/refresh our own pid file so a launchd-managed daemon (which never went
    # through service.start_daemon) is still visible to status/stop/doctor.
    pid_path = _write_daemon_pid(config)
    try:
        while True:
            # Resilience: one bad cycle must never take the daemon down. Any error in
            # event processing or maintenance is logged and the loop continues. The
            # heartbeat is only refreshed after a SUCCESSFUL cycle, so a daemon whose
            # every cycle raises goes stale and doctor reports it unhealthy instead of
            # looking alive on a perpetual failure.
            try:
                process_once(conn, config, deliver=deliver, terminal_only=terminal_only)
                run_maintenance(conn, config, current_time=now_ts())
                write_heartbeat(config)
            except Exception as exc:  # noqa: BLE001 - keep the daemon alive
                _log(f"cycle error (continuing): {exc}")
            if once:
                return
            time.sleep(config.poll_interval_ms / 1000)
            # Hot-reload config so menu-bar tweaks (speed, voice, model, toggles) take
            # effect within one poll cycle without restarting the daemon — restarting
            # on every change froze the menu bar mid-interaction.
            config, last_mtime = maybe_reload_config(config, config_path, last_mtime)
    finally:
        conn.close()
        # Clear our pid file on clean exit so the slot is not seen as a live daemon.
        if pid_path is not None:
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - defensive
                pass
