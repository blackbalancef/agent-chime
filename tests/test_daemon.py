import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_voice.config import AgentVoiceConfig, load_config, set_voice_config
from agent_voice.daemon import (
    _idle_reminder_delay_seconds,
    build_idle_reminder_message,
    deliver_due_reminders,
    in_quiet_hours,
    maybe_reload_config,
    process_once,
    run_maintenance,
)
from agent_voice.db import cancel_reminder, connect, due_reminders, init_db
from agent_voice.delivery import DeliveryResult
from agent_voice.intelligence.summarizer import SummaryResult
from agent_voice.models import EventType, NormalizedEvent
from agent_voice.queue import enqueue_event


class DaemonTests(unittest.TestCase):
    def test_multiple_events_are_grouped_into_one_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(config_path=Path(tmp) / "config.toml", database_path=db_path)
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                ),
            )
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="attention",
                    agent_name="codex",
                    event_type=EventType.PERMISSION_NEEDED,
                    project_name="web",
                    session_id="s2",
                    ask_summary="npm install",
                ),
            )

            result = process_once(conn, config, deliver=False, current_time=100)

            self.assertEqual(result.processed_events, 2)
            self.assertEqual(result.notifications_created, 1)
            row = conn.execute("SELECT * FROM notifications").fetchone()
            self.assertEqual(row["category"], "grouped_summary")
            self.assertIn("web needs attention", row["message"])
            self.assertIn("api completed", row["message"])

    def test_later_completion_suppresses_stale_attention_in_same_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(config_path=Path(tmp) / "config.toml", database_path=db_path)
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="attention",
                    agent_name="codex",
                    event_type=EventType.INPUT_NEEDED,
                    project_name="api",
                    session_id="s1",
                    ask_summary="choose an approach",
                    created_at=100,
                ),
            )
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                    created_at=101,
                ),
            )

            result = process_once(conn, config, deliver=False, current_time=200)

            self.assertEqual(result.processed_events, 2)
            row = conn.execute("SELECT * FROM notifications").fetchone()
            self.assertEqual(row["category"], "completed")
            self.assertEqual(row["message"], "Session api is fully complete.")

    def test_later_failure_replaces_completion_in_same_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(config_path=Path(tmp) / "config.toml", database_path=db_path)
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                    created_at=100,
                ),
            )
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="failed",
                    agent_name="codex",
                    event_type=EventType.TASK_FAILED,
                    project_name="api",
                    session_id="s1",
                    ask_summary="tests did not pass",
                    created_at=101,
                ),
            )

            result = process_once(conn, config, deliver=False, current_time=200)

            self.assertEqual(result.processed_events, 2)
            row = conn.execute("SELECT * FROM notifications").fetchone()
            self.assertEqual(row["category"], "failed")
            self.assertIn("tests did not pass", row["message"])

    def test_later_completion_in_same_session_still_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(config_path=Path(tmp) / "config.toml", database_path=db_path)
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="first-finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                    ask_summary="first step is ready",
                ),
            )

            first_result = process_once(conn, config, deliver=False, current_time=100)
            self.assertEqual(first_result.notifications_created, 1)

            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="second-finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                    ask_summary="second step is ready",
                ),
            )

            second_result = process_once(conn, config, deliver=False, current_time=110)

            self.assertEqual(second_result.processed_events, 1)
            self.assertEqual(second_result.notifications_created, 1)
            rows = conn.execute("SELECT * FROM notifications ORDER BY id").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertIn("second step is ready", rows[-1]["message"])

    def test_repeated_permission_in_cooldown_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(config_path=Path(tmp) / "config.toml", database_path=db_path, duplicate_cooldown_seconds=300)
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="permission-generic",
                    agent_name="codex",
                    event_type=EventType.PERMISSION_NEEDED,
                    project_name="api",
                    session_id="s1",
                ),
            )

            first_result = process_once(conn, config, deliver=False, current_time=100)
            self.assertEqual(first_result.notifications_created, 1)

            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="permission-detail",
                    agent_name="codex",
                    event_type=EventType.PERMISSION_NEEDED,
                    project_name="api",
                    session_id="s1",
                    ask_summary="Claude needs your permission",
                ),
            )

            second_result = process_once(conn, config, deliver=False, current_time=108)

            self.assertEqual(second_result.processed_events, 1)
            self.assertEqual(second_result.notifications_created, 0)
            count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
            self.assertEqual(count, 1)

    def test_delivery_usage_metrics_are_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(config_path=Path(tmp) / "config.toml", database_path=db_path)
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                ),
            )

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False) -> None:
                    pass

                def deliver(self, message: str) -> list[DeliveryResult]:
                    return [
                        DeliveryResult(
                            channel="openai_tts",
                            delivered=True,
                            spoken=True,
                            audio_generated=True,
                            audio_duration_seconds=4.5,
                            audio_cost_usd=0.001125,
                            audio_request_id="req_123",
                            audio_client_request_id="client_123",
                            audio_input_text_tokens=11,
                            audio_output_audio_tokens=94,
                            audio_input_cost_usd=0.0000066,
                            audio_output_cost_usd=0.001128,
                            audio_token_count_method="tiktoken:o200k_base",
                        )
                    ]

            with patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter):
                result = process_once(conn, config, deliver=True, current_time=100)

            self.assertEqual(result.notifications_delivered, 1)
            row = conn.execute("SELECT * FROM notifications").fetchone()
            self.assertEqual(row["channel"], "openai_tts")
            self.assertEqual(row["spoken"], 1)
            self.assertEqual(row["audio_generated"], 1)
            self.assertAlmostEqual(row["audio_duration_seconds"], 4.5)
            self.assertAlmostEqual(row["audio_cost_usd"], 0.001125)
            self.assertEqual(row["audio_request_id"], "req_123")
            self.assertEqual(row["audio_client_request_id"], "client_123")
            self.assertEqual(row["audio_input_text_tokens"], 11)
            self.assertEqual(row["audio_output_audio_tokens"], 94)
            self.assertAlmostEqual(row["audio_input_cost_usd"], 0.0000066)
            self.assertAlmostEqual(row["audio_output_cost_usd"], 0.001128)
            self.assertEqual(row["audio_token_count_method"], "tiktoken:o200k_base")
            conn.close()

    def test_idle_notification_after_completion_becomes_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(config_path=Path(tmp) / "config.toml", database_path=db_path)

            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                ),
            )
            process_once(conn, config, deliver=False, current_time=100)

            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="idle",
                    agent_name="codex",
                    event_type=EventType.INPUT_NEEDED,
                    project_name="api",
                    session_id="s1",
                    ask_summary="Here is the full result I already produced.",
                ),
            )
            process_once(conn, config, deliver=False, current_time=110)

            rows = conn.execute("SELECT message FROM notifications ORDER BY id").fetchall()
            self.assertEqual(len(rows), 2)
            reminder = rows[-1]["message"]
            # Short reminder: "<project> is waiting for your reply." (no verbose
            # cache-window phrasing), and it never leaks the assistant's message.
            self.assertIn("waiting for your reply", reminder.lower())
            self.assertIn("api", reminder)
            self.assertNotIn("full result I already produced", reminder)
            conn.close()

    def test_input_needed_is_gated_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                notify_input_needed=False,
            )
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="idle",
                    agent_name="claude-code",
                    event_type=EventType.INPUT_NEEDED,
                    project_name="api",
                    session_id="s1",
                ),
            )

            result = process_once(conn, config, deliver=False, current_time=100)

            self.assertEqual(result.processed_events, 1)
            self.assertEqual(result.notifications_created, 0)
            conn.close()

    def test_voice_is_throttled_within_min_seconds_between_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                min_seconds_between_voice_messages=8,
                quiet_hours_enabled=False,  # isolate the min-seconds throttle from quiet hours
            )
            seen_voice_enabled: list[bool] = []

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False) -> None:
                    seen_voice_enabled.append(config.voice_enabled)

                def deliver(self, message: str) -> list[DeliveryResult]:
                    if seen_voice_enabled[-1]:
                        return [DeliveryResult(channel="openai_tts", delivered=True, spoken=True, audio_generated=True)]
                    return [DeliveryResult(channel="macos_notification", delivered=True)]

            def enqueue(event_key: str) -> None:
                enqueue_event(
                    conn,
                    NormalizedEvent.build(
                        event_key=event_key,
                        agent_name="codex",
                        event_type=EventType.TASK_FINISHED,
                        project_name="api",
                        session_id="s1",  # same session — throttle applies
                    ),
                )

            with patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter):
                enqueue("first")
                process_once(conn, config, deliver=True, current_time=100)
                enqueue("second")
                process_once(conn, config, deliver=True, current_time=105)
                enqueue("third")
                process_once(conn, config, deliver=True, current_time=120)

            # First spoken; second suppressed (same session within 8s); third allowed (20s later).
            self.assertEqual(seen_voice_enabled, [True, False, True])
            rows = conn.execute("SELECT channel, spoken FROM notifications ORDER BY id").fetchall()
            self.assertEqual(rows[0]["channel"], "openai_tts")
            self.assertEqual(rows[1]["channel"], "macos_notification")
            self.assertEqual(rows[1]["spoken"], 0)
            self.assertEqual(rows[2]["channel"], "openai_tts")
            conn.close()

    def test_different_sessions_are_not_throttled_and_play_in_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                min_seconds_between_voice_messages=8,
                quiet_hours_enabled=False,  # isolate the throttle from quiet hours
            )
            seen_voice_enabled: list[bool] = []

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False) -> None:
                    seen_voice_enabled.append(config.voice_enabled)

                def deliver(self, message: str) -> list[DeliveryResult]:
                    return [DeliveryResult(channel="openai_tts", delivered=True, spoken=True, audio_generated=True)]

            def enqueue(event_key: str, session_id: str) -> None:
                enqueue_event(
                    conn,
                    NormalizedEvent.build(
                        event_key=event_key,
                        agent_name="codex",
                        event_type=EventType.TASK_FINISHED,
                        project_name=session_id,
                        session_id=session_id,
                    ),
                )

            with patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter):
                enqueue("a", "s1")
                process_once(conn, config, deliver=True, current_time=100)
                enqueue("b", "s2")  # different session, only 3s later
                process_once(conn, config, deliver=True, current_time=103)

            # Both voiced — distinct sessions are announced one after another.
            self.assertEqual(seen_voice_enabled, [True, True])
            rows = conn.execute("SELECT channel FROM notifications ORDER BY id").fetchall()
            self.assertEqual([row["channel"] for row in rows], ["openai_tts", "openai_tts"])
            conn.close()

    def test_completed_notification_is_summarized_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                summary_enabled=True,
                quiet_hours_enabled=False,  # quiet hours would suppress the voice path (and its summary)
            )
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                    summary_source_text="Done.\n\n**Changes:** added voice notification summarization.",
                ),
            )
            seen_source_text = None

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False) -> None:
                    pass

                def deliver(self, message: str) -> list[DeliveryResult]:
                    return [DeliveryResult(channel="terminal_log", delivered=True)]

            def fake_summarize(config: AgentVoiceConfig, candidate) -> SummaryResult:
                nonlocal seen_source_text
                seen_source_text = candidate.summary_source_text
                return SummaryResult(message="Voice notification summaries are ready.", cost_usd=0.000123)

            with (
                patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter),
                patch("agent_voice.daemon.summarize_notification", fake_summarize),
            ):
                result = process_once(conn, config, deliver=True, current_time=100)

            self.assertEqual(result.notifications_delivered, 1)
            self.assertEqual(seen_source_text, "Done.\n\n**Changes:** added voice notification summarization.")
            row = conn.execute("SELECT * FROM notifications").fetchone()
            self.assertEqual(row["message"], "Voice notification summaries are ready.")
            self.assertAlmostEqual(row["summary_cost_usd"], 0.000123)
            event_row = conn.execute("SELECT summary_source_text FROM events WHERE event_key = 'finished'").fetchone()
            self.assertIsNone(event_row["summary_source_text"])
            conn.close()

    def test_pipeline_log_records_source_and_spoken_text(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                summary_enabled=True,
                quiet_hours_enabled=False,  # quiet hours would suppress the voice path (and its summary)
            )
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                    summary_source_text="Full raw final message from the assistant.",
                ),
            )

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False) -> None:
                    pass

                def deliver(self, message: str) -> list[DeliveryResult]:
                    return [DeliveryResult(channel="terminal_log", delivered=True, spoken=False)]

            def fake_summarize(config: AgentVoiceConfig, candidate) -> SummaryResult:
                return SummaryResult(
                    message="Done summarizing.",
                    cost_usd=0.0001,
                    prompt="PROMPT BODY",
                    raw_text="Done summarizing",
                )

            with (
                patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter),
                patch("agent_voice.daemon.summarize_notification", fake_summarize),
            ):
                process_once(conn, config, deliver=True, current_time=100)
            conn.close()

            log_path = Path(tmp) / "summary.log"
            self.assertTrue(log_path.exists())
            record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["source_text"], "Full raw final message from the assistant.")
            self.assertEqual(record["prompt"], "PROMPT BODY")
            self.assertEqual(record["gpt_raw_output"], "Done summarizing")
            self.assertEqual(record["gpt_clean_output"], "Done summarizing.")
            self.assertIn("Done summarizing.", record["spoken_text"])
            self.assertTrue(record["gpt_used"])

    def test_pipeline_log_marks_grouped_notifications(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(config_path=Path(tmp) / "config.toml", database_path=db_path)
            for key, session in (("a", "s1"), ("b", "s2")):
                enqueue_event(
                    conn,
                    NormalizedEvent.build(
                        event_key=key,
                        agent_name="codex",
                        event_type=EventType.TASK_FINISHED,
                        project_name=key,
                        session_id=session,
                    ),
                )

            process_once(conn, config, deliver=False, current_time=100)
            conn.close()

            record = json.loads((Path(tmp) / "summary.log").read_text(encoding="utf-8").splitlines()[-1])
            self.assertTrue(record["grouped"])
            self.assertFalse(record["gpt_used"])


class ConfigHotReloadTests(unittest.TestCase):
    def test_reloads_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_voice_config(config_path, speed=1.0)
            config = load_config(config_path)
            self.assertEqual(config.voice_speed, 1.0)

            # Simulate a menu-bar tweak landing on disk.
            set_voice_config(config_path, speed=1.75)

            new_config, new_mtime = maybe_reload_config(config, config_path, None)
            self.assertEqual(new_config.voice_speed, 1.75)
            self.assertIsNotNone(new_mtime)

    def test_does_not_reload_when_mtime_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_voice_config(config_path, speed=1.0)
            config = load_config(config_path)
            mtime = config_path.stat().st_mtime

            result, result_mtime = maybe_reload_config(config, config_path, mtime)

            self.assertIs(result, config)
            self.assertEqual(result_mtime, mtime)

    def test_keeps_previous_config_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_voice_config(config_path, speed=1.0)
            config = load_config(config_path)
            config_path.unlink()

            result, result_mtime = maybe_reload_config(config, config_path, 123.0)

            self.assertIs(result, config)
            self.assertEqual(result_mtime, 123.0)


class QuietHoursTests(unittest.TestCase):
    def _config(self, **kwargs) -> AgentVoiceConfig:
        return AgentVoiceConfig(timezone="UTC", **kwargs)

    def test_disabled_is_never_quiet(self) -> None:
        config = self._config(quiet_hours_enabled=False, quiet_hours_from="23:00", quiet_hours_to="09:00")
        # 02:00 UTC would be inside the window, but quiet hours are off.
        self.assertFalse(in_quiet_hours(config, now=2 * 3600))

    def test_window_wrapping_past_midnight(self) -> None:
        config = self._config(quiet_hours_enabled=True, quiet_hours_from="23:00", quiet_hours_to="09:00")
        self.assertTrue(in_quiet_hours(config, now=23 * 3600 + 30 * 60))  # 23:30 inside
        self.assertTrue(in_quiet_hours(config, now=2 * 3600))             # 02:00 inside
        self.assertTrue(in_quiet_hours(config, now=8 * 3600 + 59 * 60))   # 08:59 inside
        self.assertFalse(in_quiet_hours(config, now=9 * 3600))            # 09:00 boundary excluded
        self.assertFalse(in_quiet_hours(config, now=12 * 3600))           # noon outside

    def test_same_day_window(self) -> None:
        config = self._config(quiet_hours_enabled=True, quiet_hours_from="13:00", quiet_hours_to="14:00")
        self.assertTrue(in_quiet_hours(config, now=13 * 3600 + 30 * 60))
        self.assertFalse(in_quiet_hours(config, now=12 * 3600))
        self.assertFalse(in_quiet_hours(config, now=14 * 3600))

    def test_quiet_hours_suppress_voice_in_process_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            # quiet_hours_voice False => voice suppressed; quiet_hours_desktop True => desktop allowed
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                timezone="UTC",
                quiet_hours_enabled=True,
                quiet_hours_from="00:00",
                quiet_hours_to="23:59",
                quiet_hours_voice=False,
            )
            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                ),
            )

            seen_voice_enabled: list[bool] = []

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False) -> None:
                    seen_voice_enabled.append(config.voice_enabled)

                def deliver(self, message: str) -> list[DeliveryResult]:
                    return [DeliveryResult(channel="macos_notification", delivered=True)]

            with (
                patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter),
                patch("agent_voice.daemon.in_quiet_hours", return_value=True),
            ):
                process_once(conn, config, deliver=True, current_time=100)

            self.assertEqual(seen_voice_enabled, [False])  # voice gated off during quiet hours
            row = conn.execute("SELECT channel, spoken, error FROM notifications").fetchone()
            self.assertEqual(row["channel"], "macos_notification")
            self.assertEqual(row["spoken"], 0)
            self.assertIn("quiet_hours", row["error"])
            conn.close()


class RateLimitTests(unittest.TestCase):
    def _seed_voice_deliveries(self, conn, *, count: int, delivered_at: int) -> None:
        for i in range(count):
            conn.execute(
                """
                INSERT INTO notifications (
                    event_ids_json, category, channel, message, notification_hash,
                    spoken, audio_generated, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("[]", "completed", "openai_tts", "Done.", f"r{delivered_at}-{i}", 1, 1, delivered_at, delivered_at),
            )
        conn.commit()

    def test_rate_limit_suppresses_voice_beyond_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                quiet_hours_enabled=False,
                min_seconds_between_voice_messages=0,
                max_events_per_minute=2,
            )
            # Two voice deliveries already in the trailing 60s => at cap.
            self._seed_voice_deliveries(conn, count=2, delivered_at=80)

            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                ),
            )

            seen_voice_enabled: list[bool] = []
            summarize_calls: list[int] = []

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False) -> None:
                    seen_voice_enabled.append(config.voice_enabled)

                def deliver(self, message: str) -> list[DeliveryResult]:
                    return [DeliveryResult(channel="macos_notification", delivered=True)]

            def fake_summarize(config, candidate):
                summarize_calls.append(1)
                from agent_voice.intelligence.summarizer import SummaryResult

                return SummaryResult(message="x", cost_usd=0.001)

            with (
                patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter),
                patch("agent_voice.daemon.summarize_notification", fake_summarize),
            ):
                process_once(conn, config, deliver=True, current_time=100)

            self.assertEqual(seen_voice_enabled, [False])  # voice suppressed at cap
            self.assertEqual(summarize_calls, [])          # no paid summary when suppressed
            row = conn.execute(
                "SELECT channel, spoken, error FROM notifications ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row["spoken"], 0)
            self.assertIn("rate_limited", row["error"])
            conn.close()

    def test_under_cap_still_voices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                quiet_hours_enabled=False,
                min_seconds_between_voice_messages=0,
                max_events_per_minute=5,
            )
            self._seed_voice_deliveries(conn, count=1, delivered_at=80)  # well under cap

            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                ),
            )

            seen_voice_enabled: list[bool] = []

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False) -> None:
                    seen_voice_enabled.append(config.voice_enabled)

                def deliver(self, message: str) -> list[DeliveryResult]:
                    return [DeliveryResult(channel="openai_tts", delivered=True, spoken=True, audio_generated=True)]

            with patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter):
                process_once(conn, config, deliver=True, current_time=100)

            self.assertEqual(seen_voice_enabled, [True])
            conn.close()


class SpendCapTests(unittest.TestCase):
    def _seed_today_spend(self, conn, *, audio_cost: float) -> None:
        from agent_voice.usage import start_of_day_epoch

        today = start_of_day_epoch("Europe/Belgrade")
        conn.execute(
            """
            INSERT INTO notifications (
                event_ids_json, category, channel, message, notification_hash,
                spoken, audio_generated, audio_cost_usd, summary_cost_usd, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("[]", "completed", "openai_tts", "Done.", "spend1", 1, 1, audio_cost, 0.0, today + 60),
        )
        conn.commit()

    def test_daily_cap_forces_free_backend_and_skips_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                timezone="Europe/Belgrade",
                quiet_hours_enabled=False,
                min_seconds_between_voice_messages=0,
                voice_backend="openai_tts",
                daily_spend_cap_usd=0.05,
            )
            self._seed_today_spend(conn, audio_cost=0.10)  # already over the $0.05 cap

            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                ),
            )

            seen_force_backend: list[object] = []
            summarize_calls: list[int] = []

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False, force_backend=None) -> None:
                    seen_force_backend.append(force_backend)

                def deliver(self, message: str) -> list[DeliveryResult]:
                    return [DeliveryResult(channel="macos_say", delivered=True, spoken=True, audio_generated=True)]

            def fake_summarize(config, candidate):
                summarize_calls.append(1)
                from agent_voice.intelligence.summarizer import SummaryResult

                return SummaryResult(message="x", cost_usd=0.001)

            with (
                patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter),
                patch("agent_voice.daemon.summarize_notification", fake_summarize),
            ):
                process_once(conn, config, deliver=True, current_time=100)

            self.assertEqual(seen_force_backend, ["macos_say"])  # forced to the free backend
            self.assertEqual(summarize_calls, [])                # paid summary skipped over the cap
            conn.close()

    def test_daily_cap_skips_summary_on_macos_say_backend(self) -> None:
        # H2: the spend cap must skip the paid GPT summary even when the configured
        # backend is the (free) default macos_say — the summary is metered too.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                timezone="Europe/Belgrade",
                quiet_hours_enabled=False,
                min_seconds_between_voice_messages=0,
                voice_backend="macos_say",  # the DEFAULT free backend
                summary_enabled=True,
                daily_spend_cap_usd=0.05,
            )
            self._seed_today_spend(conn, audio_cost=0.10)  # already over the $0.05 cap

            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                ),
            )

            seen_force_backend: list[object] = []
            summarize_calls: list[int] = []

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False, force_backend=None) -> None:
                    seen_force_backend.append(force_backend)

                def deliver(self, message: str) -> list[DeliveryResult]:
                    return [DeliveryResult(channel="macos_say", delivered=True, spoken=True, audio_generated=True)]

            def fake_summarize(config, candidate):
                summarize_calls.append(1)
                return SummaryResult(message="x", cost_usd=0.001)

            with (
                patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter),
                patch("agent_voice.daemon.summarize_notification", fake_summarize),
            ):
                process_once(conn, config, deliver=True, current_time=100)

            # Paid summary skipped over the cap on the free backend.
            self.assertEqual(summarize_calls, [])
            # No force_backend is passed: the backend is already the free macos_say,
            # so there is nothing to force away from.
            self.assertEqual(seen_force_backend, [None])
            conn.close()

    def test_under_cap_keeps_paid_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                timezone="Europe/Belgrade",
                quiet_hours_enabled=False,
                min_seconds_between_voice_messages=0,
                voice_backend="openai_tts",
                daily_spend_cap_usd=1.0,
            )
            self._seed_today_spend(conn, audio_cost=0.10)  # under the $1.00 cap

            enqueue_event(
                conn,
                NormalizedEvent.build(
                    event_key="finished",
                    agent_name="codex",
                    event_type=EventType.TASK_FINISHED,
                    project_name="api",
                    session_id="s1",
                ),
            )

            seen_force_backend: list[object] = []

            class FakeDeliveryRouter:
                def __init__(self, config, *, terminal_only: bool = False, force_backend=None) -> None:
                    seen_force_backend.append(force_backend)

                def deliver(self, message: str) -> list[DeliveryResult]:
                    return [DeliveryResult(channel="openai_tts", delivered=True, spoken=True, audio_generated=True)]

            with patch("agent_voice.daemon.DeliveryRouter", FakeDeliveryRouter):
                process_once(conn, config, deliver=True, current_time=100)

            # force_backend is None => not passed at all; fake default keeps None
            self.assertEqual(seen_force_backend, [None])
            conn.close()


class MaintenanceTests(unittest.TestCase):
    def test_retention_prunes_old_processed_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                event_retention_days=30,
            )
            now = 100 * 86400  # day 100
            old = now - 40 * 86400  # 40 days ago -> prunable
            recent = now - 5 * 86400  # 5 days ago -> kept
            conn.execute(
                "INSERT INTO events (event_key, agent_name, event_type, raw_payload_json, status, created_at) "
                "VALUES (?, ?, ?, ?, 'processed', ?)",
                ("old", "codex", "task_finished", "{}", old),
            )
            conn.execute(
                "INSERT INTO events (event_key, agent_name, event_type, raw_payload_json, status, created_at) "
                "VALUES (?, ?, ?, ?, 'processed', ?)",
                ("recent", "codex", "task_finished", "{}", recent),
            )
            conn.commit()

            pruned = run_maintenance(conn, config, current_time=now, now=1000.0)

            self.assertEqual(pruned, 1)
            keys = {row[0] for row in conn.execute("SELECT event_key FROM events").fetchall()}
            self.assertEqual(keys, {"recent"})
            conn.close()

    def test_retention_disabled_keeps_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                event_retention_days=0,  # keep forever
            )
            conn.execute(
                "INSERT INTO events (event_key, agent_name, event_type, raw_payload_json, status, created_at) "
                "VALUES (?, ?, ?, ?, 'processed', ?)",
                ("ancient", "codex", "task_finished", "{}", 0),
            )
            conn.commit()

            pruned = run_maintenance(conn, config, current_time=100 * 86400, now=1000.0)

            self.assertEqual(pruned, 0)
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            self.assertEqual(count, 1)
            conn.close()

    def test_vacuum_baselines_then_runs_after_interval(self) -> None:
        from agent_voice import daemon as daemon_module
        from agent_voice.runtime import read_runtime_state

        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
                event_retention_days=0,
            )

            vacuum_calls: list[int] = []
            with patch.object(daemon_module, "vacuum_db", lambda c: vacuum_calls.append(1)):
                # First cycle baselines without vacuuming.
                run_maintenance(conn, config, current_time=0, now=1000.0)
                self.assertEqual(vacuum_calls, [])
                self.assertAlmostEqual(read_runtime_state(config)["last_vacuum_at"], 1000.0)

                # Still within the 24h interval: no vacuum.
                run_maintenance(conn, config, current_time=0, now=1000.0 + 3600)
                self.assertEqual(vacuum_calls, [])

                # Past the interval: vacuum runs and the timestamp advances.
                later = 1000.0 + daemon_module.VACUUM_INTERVAL_SECONDS + 1
                run_maintenance(conn, config, current_time=0, now=later)
                self.assertEqual(vacuum_calls, [1])
                self.assertAlmostEqual(read_runtime_state(config)["last_vacuum_at"], later)
            conn.close()


class HeartbeatTests(unittest.TestCase):
    def test_write_read_and_age(self) -> None:
        from agent_voice.heartbeat import (
            heartbeat_age_seconds,
            heartbeat_path,
            read_heartbeat,
            write_heartbeat,
        )

        with tempfile.TemporaryDirectory() as tmp:
            config = AgentVoiceConfig(config_path=Path(tmp) / "config.toml")

            self.assertIsNone(read_heartbeat(config))
            self.assertIsNone(heartbeat_age_seconds(config))

            write_heartbeat(config, now=1000.0)
            self.assertEqual(heartbeat_path(config).name, "daemon.heartbeat")

            heartbeat = read_heartbeat(config)
            self.assertIsNotNone(heartbeat)
            self.assertEqual(heartbeat["ts"], 1000)
            self.assertIn("pid", heartbeat)
            self.assertIn("version", heartbeat)

            self.assertAlmostEqual(heartbeat_age_seconds(config, now=1005.0), 5.0)
            # Clock skew never yields a negative age.
            self.assertEqual(heartbeat_age_seconds(config, now=900.0), 0.0)

    def test_corrupt_heartbeat_reads_as_none(self) -> None:
        from agent_voice.heartbeat import heartbeat_path, read_heartbeat

        with tempfile.TemporaryDirectory() as tmp:
            config = AgentVoiceConfig(config_path=Path(tmp) / "config.toml")
            path = heartbeat_path(config)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{ not json", encoding="utf-8")
            self.assertIsNone(read_heartbeat(config))


class ResilienceTests(unittest.TestCase):
    def test_run_daemon_continues_after_a_bad_cycle(self) -> None:
        from agent_voice import daemon as daemon_module

        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            conn.close()
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
            )

            with patch.object(daemon_module, "process_once", side_effect=RuntimeError("boom")):
                # once=True runs a single cycle; the exception must be swallowed
                # (run_daemon returns without raising — the daemon stays alive).
                daemon_module.run_daemon(config, once=True, deliver=False)

    def test_perpetually_failing_cycle_does_not_refresh_heartbeat(self) -> None:
        # M3: write_heartbeat runs only after a SUCCESSFUL cycle, so a daemon whose
        # every cycle raises does not look healthy to doctor.
        from agent_voice import daemon as daemon_module
        from agent_voice.heartbeat import read_heartbeat

        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            conn.close()
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
            )

            with patch.object(daemon_module, "process_once", side_effect=RuntimeError("boom")):
                daemon_module.run_daemon(config, once=True, deliver=False)

            # No heartbeat was written because the cycle never completed.
            self.assertIsNone(read_heartbeat(config))

    def test_successful_cycle_refreshes_heartbeat(self) -> None:
        from agent_voice import daemon as daemon_module
        from agent_voice.heartbeat import read_heartbeat

        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            conn.close()
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
            )

            daemon_module.run_daemon(config, once=True, deliver=False)

            heartbeat = read_heartbeat(config)
            self.assertIsNotNone(heartbeat)
            self.assertIn("pid", heartbeat)
            self.assertIn("ts", heartbeat)


class DaemonPidFileTests(unittest.TestCase):
    def test_run_daemon_writes_pid_file_during_run(self) -> None:
        # H4: a launchd-managed daemon (started without service.start_daemon) must
        # still write its own pid file so status/stop/doctor can see it.
        import os as _os

        from agent_voice import daemon as daemon_module
        from agent_voice.service import service_paths

        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/events.sqlite3"
            conn = connect(db_path)
            init_db(conn)
            conn.close()
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=db_path,
            )
            pid_path = service_paths(config).pid_path

            seen_pid: list[str | None] = []

            def _record_pid(*args, **kwargs):
                # Captured mid-run, before the finally block clears the pid file.
                seen_pid.append(
                    pid_path.read_text(encoding="utf-8") if pid_path.exists() else None
                )
                return None

            with patch.object(daemon_module, "process_once", side_effect=_record_pid):
                daemon_module.run_daemon(config, once=True, deliver=False)

            self.assertEqual(seen_pid, [str(_os.getpid())])
            # Clean exit clears the pid file so the slot is not seen as a live daemon.
            self.assertFalse(pid_path.exists())


class TimedIdleReminderTests(unittest.TestCase):
    def _config(self, tmp: str, **overrides) -> AgentVoiceConfig:
        return AgentVoiceConfig(
            config_path=Path(tmp) / "config.toml",
            database_path=f"{tmp}/events.sqlite3",
            voice_backend="macos_say",
            **overrides,
        )

    def _finish(self, conn, session_id="s1", project="api", agent="claude-code", *, current_time):
        enqueue_event(
            conn,
            NormalizedEvent.build(
                event_key=f"{session_id}-{current_time}",
                agent_name=agent,
                event_type=EventType.TASK_FINISHED,
                project_name=project,
                session_id=session_id,
            ),
        )
        process_once(conn, self._cfg, deliver=True, terminal_only=True, current_time=current_time)

    def test_delay_uses_cache_window_minus_margin(self) -> None:
        cfg = AgentVoiceConfig(idle_reminder_margin_minutes=1)
        self.assertEqual(_idle_reminder_delay_seconds(cfg, "claude-code"), 4 * 60)  # 5-1
        self.assertEqual(_idle_reminder_delay_seconds(cfg, "codex"), 9 * 60)  # 10-1

    def test_message_is_short_and_localized(self) -> None:
        cfg_ru = AgentVoiceConfig(language="ru")
        self.assertEqual(build_idle_reminder_message(cfg_ru, "claude-code", "api"), "api ждёт твоего ответа.")
        cfg_en = AgentVoiceConfig(language="en")
        self.assertEqual(build_idle_reminder_message(cfg_en, "claude-code", "api"), "api is waiting for your reply.")

    def test_finish_schedules_reminder_that_fires_once_when_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._cfg = self._config(tmp)
            conn = connect(self._cfg.database_path)
            init_db(conn)
            self._finish(conn, current_time=1000)

            self.assertEqual(len(due_reminders(conn, 1100)), 0)  # not yet (needs +240)
            self.assertEqual(len(due_reminders(conn, 1240)), 1)  # 4 min later

            delivered = deliver_due_reminders(conn, self._cfg, current_time=1240, terminal_only=True)
            self.assertEqual(delivered, 1)
            self.assertEqual(len(due_reminders(conn, 999999)), 0)  # one-shot
            msg = conn.execute("SELECT message FROM notifications ORDER BY id DESC LIMIT 1").fetchone()[0]
            self.assertIn("waiting for your reply", msg)
            conn.close()

    def test_disabled_does_not_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._cfg = self._config(tmp, idle_reminder_enabled=False)
            conn = connect(self._cfg.database_path)
            init_db(conn)
            self._finish(conn, current_time=1000)
            self.assertEqual(len(due_reminders(conn, 999999)), 0)
            conn.close()

    def test_reply_cancels_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._cfg = self._config(tmp)
            conn = connect(self._cfg.database_path)
            init_db(conn)
            self._finish(conn, current_time=1000)
            self.assertEqual(len(due_reminders(conn, 1240)), 1)
            cancel_reminder(conn, "s1")  # user replied
            self.assertEqual(len(due_reminders(conn, 1240)), 0)
            conn.close()

    def test_quiet_hours_suppresses_reminder_voice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Quiet window covering the whole day so the reminder is always inside it.
            self._cfg = self._config(
                tmp,
                quiet_hours_enabled=True,
                quiet_hours_from="00:00",
                quiet_hours_to="23:59",
                quiet_hours_voice=False,
            )
            conn = connect(self._cfg.database_path)
            init_db(conn)
            self._finish(conn, current_time=1000)
            deliver_due_reminders(conn, self._cfg, current_time=1240)
            row = conn.execute(
                "SELECT spoken, audio_cost_usd FROM notifications ORDER BY id DESC LIMIT 1"
            ).fetchone()
            # Voice is silenced in quiet hours: the reminder is recorded but not spoken.
            self.assertEqual(row["spoken"], 0)
            self.assertEqual(row["audio_cost_usd"], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
