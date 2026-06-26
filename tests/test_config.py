import tempfile
import tomllib
import unittest
from pathlib import Path

from agent_voice.config import (
    CONFIG_SCHEMA_VERSION,
    AgentVoiceConfig,
    ConfigError,
    language_display_name,
    list_config_backups,
    load_config,
    normalize_hhmm,
    reset_config,
    restore_config_backup,
    set_autostart_managed,
    set_config_language,
    set_daemon_config,
    set_events_config,
    set_limits_config,
    set_quiet_hours_config,
    set_summary_config,
    set_voice_config,
    write_default_config,
)
from agent_voice.config import _atomic_write_config, _backup_config


class ConfigTests(unittest.TestCase):
    def test_set_config_language_updates_user_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_config_language(config_path, "en")
            self.assertEqual(load_config(config_path).language, "en")

            set_config_language(config_path, "english")
            self.assertEqual(load_config(config_path).language, "en")

    def test_set_config_language_accepts_russian(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_config_language(config_path, "ru")
            self.assertEqual(load_config(config_path).language, "ru")

            set_config_language(config_path, "russian")
            self.assertEqual(load_config(config_path).language, "ru")

            set_config_language(config_path, "русский")
            self.assertEqual(load_config(config_path).language, "ru")

    def test_set_config_language_accepts_custom_language_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_config_language(config_path, "Spanish")
            self.assertEqual(load_config(config_path).language, "Spanish")
            self.assertEqual(language_display_name(load_config(config_path).language), "Spanish")

    def test_set_config_language_escapes_custom_language_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_config_language(config_path, 'Portuguese "Brazil"')

            self.assertEqual(load_config(config_path).language, 'Portuguese "Brazil"')

    def test_set_config_language_rejects_empty_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            with self.assertRaises(ValueError):
                set_config_language(config_path, " ")

    def test_default_message_templates_include_russian(self) -> None:
        config = AgentVoiceConfig()

        self.assertEqual(
            set(config.message_templates["ru"]),
            set(config.message_templates["en"]),
        )
        self.assertEqual(
            config.message_templates["ru"]["completed"],
            "Сессия {project} полностью завершена.",
        )

    def test_set_voice_config_updates_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_voice_config(
                config_path,
                backend="openai_tts",
                voice="marin",
                speed=1.2,
                model="gpt-4o-mini-tts",
                audio_format="mp3",
                estimated_cost_per_minute_usd=0.0123456,
                text_input_price_per_million_tokens_usd=0.6,
                audio_output_price_per_million_tokens_usd=12.0,
                audio_tokens_per_second=21.25,
                instructions="Speak calmly.",
            )
            config = load_config(config_path)

            self.assertEqual(config.voice_backend, "openai_tts")
            self.assertEqual(config.voice_name, "marin")
            self.assertEqual(config.voice_speed, 1.2)
            self.assertEqual(config.voice_model, "gpt-4o-mini-tts")
            self.assertEqual(config.voice_format, "mp3")
            self.assertEqual(config.voice_estimated_cost_per_minute_usd, 0.012346)
            self.assertEqual(config.voice_text_input_price_per_million_tokens_usd, 0.6)
            self.assertEqual(config.voice_audio_output_price_per_million_tokens_usd, 12.0)
            self.assertEqual(config.voice_audio_tokens_per_second, 21.25)
            self.assertEqual(config.voice_instructions, "Speak calmly.")

    def test_summary_is_enabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            config = load_config(config_path)
            self.assertTrue(config.summary_enabled)

    def test_set_summary_config_updates_model_and_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_summary_config(config_path, enabled=False, model="gpt-4o-mini")
            config = load_config(config_path)

            self.assertFalse(config.summary_enabled)
            self.assertEqual(config.summary_model, "gpt-4o-mini")

            set_summary_config(config_path, enabled=True)
            config = load_config(config_path)
            self.assertTrue(config.summary_enabled)
            self.assertEqual(config.summary_model, "gpt-4o-mini")

    def test_set_events_config_toggles_input_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_events_config(config_path, input_needed=False)
            config = load_config(config_path)
            self.assertFalse(config.notify_input_needed)
            self.assertTrue(config.notify_task_finished)

            set_events_config(config_path, input_needed=True)
            config = load_config(config_path)
            self.assertTrue(config.notify_input_needed)

    def test_load_config_reads_custom_message_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[user]
language = "en"

[daemon]
database_path = "events.sqlite3"

[messages.en]
attention_required = "Human input needed: {project}{reason_clause}."
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(
                config.message_templates["en"]["attention_required"],
                "Human input needed: {project}{reason_clause}.",
            )
            self.assertEqual(
                config.message_templates["en"]["completed"],
                "Session {project} is fully complete.",
            )

    def test_write_default_config_appends_message_sections_to_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[user]
language = "en"
""",
                encoding="utf-8",
            )

            write_default_config(config_path)
            text = config_path.read_text(encoding="utf-8")

            self.assertIn("[messages.en]", text)
            self.assertIn('attention_required = "{agent} in {project} needs attention{reason_clause}."', text)

    def test_write_default_config_appends_summary_prompt_to_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[user]
language = "en"

[summary]
enabled = true
""",
                encoding="utf-8",
            )

            write_default_config(config_path)
            text = config_path.read_text(encoding="utf-8")
            config = load_config(config_path)

            self.assertIn("prompt = '''", text)
            self.assertIn("text_input_price_per_million_tokens_usd", text)
            self.assertTrue(config.summary_enabled)
            self.assertEqual(config.summary_model, "gpt-5.4-nano")
            self.assertEqual(config.summary_privacy_level, "full_last_message")
            self.assertEqual(config.summary_max_input_chars, 6000)


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_rejects_invalid_toml_and_leaves_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            original = config_path.read_text(encoding="utf-8")

            with self.assertRaises(ConfigError):
                _atomic_write_config(config_path, "this is = = not [[[ valid toml")

            # The original file is untouched, and no temp leftovers remain.
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            leftovers = [p for p in Path(tmp).iterdir() if p.suffix == ".tmp"]
            self.assertEqual(leftovers, [])

    def test_atomic_write_backs_up_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)

            _atomic_write_config(config_path, 'a = 1\n', backup=True)

            backups = list(Path(tmp).glob("config.toml.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("[user]", backups[0].read_text(encoding="utf-8"))
            self.assertEqual(config_path.read_text(encoding="utf-8"), "a = 1\n")

    def test_atomic_write_sets_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            _atomic_write_config(config_path, "a = 1\n", backup=False)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_setters_route_through_atomic_write(self) -> None:
        # A successful setter must not leave any temp files behind.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_voice_config(config_path, speed=1.5)
            leftovers = [p for p in Path(tmp).iterdir() if p.suffix == ".tmp"]
            self.assertEqual(leftovers, [])


class ConfigErrorTests(unittest.TestCase):
    def test_load_config_raises_config_error_on_malformed_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("a = [1, 2\nb = 3\n", encoding="utf-8")

            with self.assertRaises(ConfigError) as ctx:
                load_config(config_path)

            error = ctx.exception
            # load_config resolves the path, so compare resolved forms.
            self.assertEqual(error.path, config_path.resolve())
            self.assertEqual(error.line, 2)
            self.assertIn("line 2", str(error))
            self.assertTrue(error.hint)

    def test_editor_raises_config_error_on_malformed_existing_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("not = = valid [[[\n", encoding="utf-8")

            with self.assertRaises(ConfigError):
                set_voice_config(config_path, speed=1.0)

    def test_load_config_raises_config_error_on_out_of_range_quiet_hours(self) -> None:
        # A TOML-valid file with an out-of-range clock time must surface as a
        # ConfigError (single error contract), not a raw ValueError.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[quiet_hours]\n"
                'from = "25:99"\n'
                'to = "09:00"\n',
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)


class MultilineSafeEditorTests(unittest.TestCase):
    def test_editing_voice_preserves_custom_multiline_summary_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            # A custom prompt whose interior lines look like keys and sections.
            config_path.write_text(
                "[summary]\n"
                "enabled = true\n"
                "prompt = '''\n"
                "Custom line one\n"
                "[voice]\n"
                'backend = "evil"\n'
                "[fake.section]\n"
                'key = "value"\n'
                "'''\n"
                "model = \"gpt-5.4-nano\"\n"
                "\n"
                "[voice]\n"
                "enabled = true\n"
                'backend = "macos_say"\n',
                encoding="utf-8",
            )
            before = tomllib.loads(config_path.read_text(encoding="utf-8"))
            prompt_before = before["summary"]["prompt"]

            set_voice_config(config_path, backend="openai_tts")

            after = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(after["voice"]["backend"], "openai_tts")
            # The multi-line prompt is preserved byte-for-byte.
            self.assertEqual(after["summary"]["prompt"], prompt_before)
            # No interior line leaked out as a real section.
            self.assertNotIn("fake", after)

    def test_replacing_a_multiline_value_drops_old_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[summary]\n"
                "enabled = true\n"
                "prompt = '''\n"
                "old body line\n"
                "'''\n"
                "model = \"gpt-5.4-nano\"\n",
                encoding="utf-8",
            )

            set_summary_config(config_path, model="gpt-4o-mini")

            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(data["summary"]["model"], "gpt-4o-mini")
            self.assertIn("old body line", data["summary"]["prompt"])

    def test_editing_voice_with_earlier_triple_quote_substring_value(self) -> None:
        # An earlier single-line value whose *text* contains ``'''`` must not be
        # mistaken for the opener of a multi-line block. Editing a later key in the
        # same section must replace it in place — not lose the edit or duplicate the
        # key at EOF — and the substring value must be left intact.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[voice]\n"
                "enabled = true\n"
                'instructions = "use \'\'\' for blocks"\n'
                "rate = 185\n",
                encoding="utf-8",
            )

            set_voice_config(config_path, rate=200)

            text = config_path.read_text(encoding="utf-8")
            data = tomllib.loads(text)
            self.assertEqual(data["voice"]["rate"], 200)
            # The triple-quote substring value survives unchanged.
            self.assertEqual(data["voice"]["instructions"], "use ''' for blocks")
            # ``rate`` is replaced in place, not duplicated at the end of the file.
            self.assertEqual(text.count("rate ="), 1)

    def test_setting_triple_quote_instructions_then_editing_another_key(self) -> None:
        # Storing an instructions value that contains ``'''`` (which sorts BEFORE a
        # later key in the same section) must not poison the edit of that later key:
        # the earlier substring value must not open a phantom multi-line block.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[voice]\n"
                "enabled = true\n",
                encoding="utf-8",
            )

            set_voice_config(config_path, instructions="use ''' for blocks")
            set_voice_config(config_path, interrupt_on_user_input=False)

            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(data["voice"]["instructions"], "use ''' for blocks")
            self.assertFalse(data["voice"]["interrupt_on_user_input"])

    def test_reset_section_with_earlier_triple_quote_substring_value(self) -> None:
        # Resetting a section while an EARLIER section holds a value whose text
        # contains ``'''`` must not swallow the reset target into a phantom block.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[voice]\n"
                "enabled = true\n"
                'instructions = "use \'\'\' for blocks"\n'
                "\n"
                "[summary]\n"
                'model = "gpt-4o-mini"\n',
                encoding="utf-8",
            )

            reset_config(config_path, section="summary")
            config = load_config(config_path)

            self.assertEqual(config.summary_model, "gpt-5.4-nano")
            self.assertEqual(config.voice_instructions, "use ''' for blocks")


class MigrationTests(unittest.TestCase):
    def test_migration_back_fills_missing_keys_and_stamps_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('[user]\nlanguage = "en"\n', encoding="utf-8")

            config = load_config(config_path)
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(data["meta"]["schema_version"], CONFIG_SCHEMA_VERSION)
            self.assertIn("event_retention_days", data["daemon"])
            self.assertIn("daily_spend_cap_usd", data["limits"])
            self.assertIn("managed", data["autostart"])
            self.assertIn("pipeline_log", data["summary"])
            self.assertEqual(config.event_retention_days, 30)

    def test_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('[user]\nlanguage = "en"\n', encoding="utf-8")

            load_config(config_path)
            after_first = config_path.read_text(encoding="utf-8")
            load_config(config_path)
            after_second = config_path.read_text(encoding="utf-8")

            self.assertEqual(after_first, after_second)

    def test_migration_preserves_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[limits]\nmax_events_per_minute = 99\n", encoding="utf-8"
            )

            config = load_config(config_path)
            self.assertEqual(config.max_events_per_minute, 99)

    def test_migration_upgrades_stale_idle_reminder_text(self) -> None:
        from agent_voice.config import _STALE_IDLE_REMINDER_DEFAULTS

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            old = _STALE_IDLE_REMINDER_DEFAULTS["ru"]
            config_path.write_text(
                f'[messages.ru]\nidle_reminder = "{old}"\n', encoding="utf-8"
            )

            config = load_config(config_path)
            # The verbose old default is replaced with the short current one.
            self.assertEqual(config.message_templates["ru"]["idle_reminder"], "{project} ждёт твоего ответа.")
            self.assertNotIn("кэш", config_path.read_text(encoding="utf-8"))

    def test_migration_keeps_custom_idle_reminder_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                '[messages.ru]\nidle_reminder = "мой текст {project}"\n', encoding="utf-8"
            )
            config = load_config(config_path)
            # A user's custom text (not the old default) is never touched.
            self.assertEqual(config.message_templates["ru"]["idle_reminder"], "мой текст {project}")

    def test_migration_upgrades_stale_summary_prompt(self) -> None:
        from agent_voice.config import (
            DEFAULT_SUMMARY_PROMPT,
            _STALE_SUMMARY_PROMPTS,
            set_config_section_values,
            write_default_config,
        )

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            set_config_section_values(config_path, "summary", {"prompt": _STALE_SUMMARY_PROMPTS[0]})

            config = load_config(config_path)
            self.assertEqual(config.summary_prompt.strip(), DEFAULT_SUMMARY_PROMPT.strip())
            self.assertIn("Always start the notification with the project name", config.summary_prompt)

    def test_migration_keeps_custom_summary_prompt(self) -> None:
        from agent_voice.config import set_config_section_values, write_default_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            set_config_section_values(config_path, "summary", {"prompt": "My custom prompt {message}"})

            config = load_config(config_path)
            self.assertEqual(config.summary_prompt, "My custom prompt {message}")
            self.assertEqual(config.daily_spend_cap_usd, 0.0)


class NewConfigKeysTests(unittest.TestCase):
    def test_quiet_hours_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            config = load_config(config_path)

            self.assertTrue(config.quiet_hours_enabled)
            self.assertEqual(config.quiet_hours_from, "23:00")
            self.assertEqual(config.quiet_hours_to, "09:00")
            self.assertFalse(config.quiet_hours_voice)
            self.assertTrue(config.quiet_hours_desktop)

    def test_default_config_has_new_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            config = load_config(config_path)

            self.assertEqual(config.daily_spend_cap_usd, 0.0)
            self.assertEqual(config.monthly_spend_cap_usd, 0.0)
            self.assertEqual(config.event_retention_days, 30)
            self.assertEqual(config.max_log_bytes, 5_000_000)
            self.assertTrue(config.summary_pipeline_log)
            self.assertFalse(config.autostart_managed)
            self.assertEqual(config.max_events_per_minute, 6)

    def test_set_limits_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_limits_config(
                config_path,
                max_events_per_minute=12,
                daily_spend_cap_usd=2.5,
                monthly_spend_cap_usd=50.0,
            )
            config = load_config(config_path)

            self.assertEqual(config.max_events_per_minute, 12)
            self.assertEqual(config.daily_spend_cap_usd, 2.5)
            self.assertEqual(config.monthly_spend_cap_usd, 50.0)

    def test_set_daemon_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_daemon_config(config_path, event_retention_days=7, max_log_bytes=1234)
            config = load_config(config_path)

            self.assertEqual(config.event_retention_days, 7)
            self.assertEqual(config.max_log_bytes, 1234)

    def test_set_daemon_config_zero_retention_keeps_forever(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_daemon_config(config_path, event_retention_days=0)
            self.assertEqual(load_config(config_path).event_retention_days, 0)

    def test_set_autostart_managed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_autostart_managed(config_path, True)
            self.assertTrue(load_config(config_path).autostart_managed)

            set_autostart_managed(config_path, False)
            self.assertFalse(load_config(config_path).autostart_managed)

    def test_set_summary_config_privacy_and_pipeline_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_summary_config(
                config_path, privacy_level="metadata_only", pipeline_log=False
            )
            config = load_config(config_path)

            self.assertEqual(config.summary_privacy_level, "metadata_only")
            self.assertFalse(config.summary_pipeline_log)

    def test_set_events_config_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            set_events_config(config_path, subagent_finished=True)
            self.assertTrue(load_config(config_path).notify_subagent_finished)


class NormalizeHHMMTests(unittest.TestCase):
    def test_normalize_hhmm_pads_and_validates(self) -> None:
        self.assertEqual(normalize_hhmm("9:05"), "09:05")
        self.assertEqual(normalize_hhmm("23:00"), "23:00")
        self.assertEqual(normalize_hhmm(" 7:30 "), "07:30")

    def test_normalize_hhmm_rejects_invalid(self) -> None:
        for bad in ("25:00", "12:60", "noon", "1230", "12:5"):
            with self.assertRaises(ValueError):
                normalize_hhmm(bad)


class ResetConfigTests(unittest.TestCase):
    def test_reset_whole_file_returns_backup_and_restores_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_voice_config(config_path, backend="openai_tts", voice="marin")

            backup = reset_config(config_path)

            self.assertTrue(backup.exists())
            self.assertTrue(backup.name.startswith("config.toml.bak-"))
            self.assertIn("openai_tts", backup.read_text(encoding="utf-8"))
            self.assertEqual(load_config(config_path).voice_backend, "macos_say")

    def test_reset_single_section_leaves_others_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_voice_config(config_path, backend="openai_tts")
            set_summary_config(config_path, model="gpt-4o-mini")

            reset_config(config_path, section="voice")
            config = load_config(config_path)

            self.assertEqual(config.voice_backend, "macos_say")
            self.assertEqual(config.summary_model, "gpt-4o-mini")

    def test_reset_summary_section_restores_multiline_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_summary_config(config_path, model="gpt-4o-mini")

            reset_config(config_path, section="summary")
            config = load_config(config_path)

            self.assertEqual(config.summary_model, "gpt-5.4-nano")
            self.assertIn("Rewrite", config.summary_prompt)

    def test_reset_unknown_section_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with self.assertRaises(ValueError):
                reset_config(config_path, section="does_not_exist")


class QuietHoursSetterTests(unittest.TestCase):
    def test_disable_and_set_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_quiet_hours_config(config_path, enabled=False)
            self.assertFalse(load_config(config_path).quiet_hours_enabled)

            set_quiet_hours_config(
                config_path, enabled=True, start="22:30", end="08:00", voice=True
            )
            config = load_config(config_path)
            self.assertTrue(config.quiet_hours_enabled)
            self.assertEqual(config.quiet_hours_from, "22:30")
            self.assertEqual(config.quiet_hours_to, "08:00")
            self.assertTrue(config.quiet_hours_voice)

    def test_invalid_time_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with self.assertRaises(ValueError):
                set_quiet_hours_config(config_path, start="25:99")


class ConfigBackupRestoreTests(unittest.TestCase):
    def test_same_second_backups_do_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            first = _backup_config(config_path)
            second = _backup_config(config_path)
            self.assertNotEqual(first, second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_list_backups_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            older = config_path.with_name("config.toml.bak-20200101000000")
            newer = config_path.with_name("config.toml.bak-20300101000000")
            older.write_text("x", encoding="utf-8")
            newer.write_text("y", encoding="utf-8")
            names = [b.name for b in list_config_backups(config_path)]
            self.assertEqual(names[0], newer.name)
            self.assertIn(older.name, names)

    def test_restore_newest_recovers_prior_content_and_backs_up_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_voice_config(config_path, voice="marin")  # writes a backup of the prior file
            # Mutate again so a backup capturing voice="marin" exists.
            set_voice_config(config_path, voice="cedar")
            backups_before = list_config_backups(config_path)
            self.assertTrue(backups_before)

            restored = restore_config_backup(config_path, backups_before[0])
            self.assertTrue(restored.exists())
            # Restoring writes a fresh backup of the just-replaced file.
            self.assertGreater(len(list_config_backups(config_path)), len(backups_before))
            # The restored file parses and is the chosen backup's content.
            self.assertEqual(
                config_path.read_text(encoding="utf-8"),
                restored.read_text(encoding="utf-8"),
            )

    def test_list_backups_orders_same_second_counters_numerically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            stamp = "20300101000000"
            base = config_path.with_name(f"config.toml.bak-{stamp}")
            two = config_path.with_name(f"config.toml.bak-{stamp}-2")
            ten = config_path.with_name(f"config.toml.bak-{stamp}-10")
            for p in (base, two, ten):
                p.write_text("x", encoding="utf-8")
            names = [b.name for b in list_config_backups(config_path)]
            # -10 was written after -2, so it must sort newest despite lexical order.
            self.assertEqual(names[0], ten.name)
            self.assertLess(names.index(ten.name), names.index(two.name))
            self.assertLess(names.index(two.name), names.index(base.name))

    def test_restore_without_backups_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with self.assertRaises(ConfigError):
                restore_config_backup(config_path)


if __name__ == "__main__":
    unittest.main()
