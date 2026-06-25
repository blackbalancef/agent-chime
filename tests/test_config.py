import tempfile
import unittest
from pathlib import Path

from agent_voice.config import (
    AgentVoiceConfig,
    language_display_name,
    load_config,
    set_config_language,
    set_events_config,
    set_summary_config,
    set_voice_config,
    write_default_config,
)


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


if __name__ == "__main__":
    unittest.main()
