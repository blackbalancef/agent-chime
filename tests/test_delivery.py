import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_voice.config import AgentVoiceConfig
from agent_voice.delivery.router import DEFAULT_TEST_MESSAGE, DeliveryResult, DeliveryRouter, test_message
from agent_voice.runtime import read_voice_activity_started_at, request_voice_stop, set_voice_mute


class TestMessageTests(unittest.TestCase):
    def test_uses_template_when_present(self) -> None:
        config = AgentVoiceConfig(message_templates={"en": {"test": "Custom check."}}, language="en")
        self.assertEqual(test_message(config), "Custom check.")

    def test_falls_back_when_template_missing(self) -> None:
        config = AgentVoiceConfig(message_templates={}, language="en")
        self.assertEqual(test_message(config), DEFAULT_TEST_MESSAGE)


class DeliveryTests(unittest.TestCase):
    def test_openai_tts_reports_missing_api_key(self) -> None:
        env_name = "VOICCCE_TEST_OPENAI_KEY"
        os.environ.pop(env_name, None)
        config = AgentVoiceConfig(
            voice_backend="openai_tts",
            voice_name="marin",
            voice_api_key_env=env_name,
            voice_api_key_keychain_service="voiccce-test-missing",
            voice_api_key_keychain_account="openai-test-missing",
        )

        result = DeliveryRouter(config)._openai_tts("Test.", started_at=0)

        self.assertFalse(result.delivered)
        self.assertEqual(result.channel, "openai_tts")
        self.assertIn(env_name, result.error or "")

    def test_voice_mute_skips_voice_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                voice_backend="openai_tts",
            )
            set_voice_mute(config, 60)

            result = DeliveryRouter(config)._voice("Test.")

            self.assertFalse(result.delivered)
            self.assertEqual(result.channel, "voice_muted")

    def test_configured_voice_does_not_fallback_to_say(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                voice_backend="openai_tts",
                voice_name="coral",
            )
            router = DeliveryRouter(config)

            with (
                patch(
                    "agent_voice.delivery.router.resolve_openai_api_key",
                    return_value=(None, SimpleNamespace(source="missing")),
                ),
                patch.object(router, "_say") as say,
            ):
                result = router.deliver_configured_voice("Test.")

            self.assertFalse(result.delivered)
            self.assertEqual(result.channel, "openai_tts")
            say.assert_not_called()

    def test_voice_stop_request_prevents_playback_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                voice_backend="macos_say",
            )
            request_voice_stop(config, now=200.0)

            with (
                patch("agent_voice.delivery.router.time.time", return_value=100.0),
                patch("agent_voice.delivery.router.shutil.which", return_value="/usr/bin/say"),
                patch("agent_voice.delivery.router.subprocess.Popen") as popen,
            ):
                result = DeliveryRouter(config)._voice("Test.")

            self.assertFalse(result.delivered)
            self.assertEqual(result.channel, "voice_cancelled")
            popen.assert_not_called()

    def test_voice_activity_is_cleared_after_voice_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                voice_backend="macos_say",
            )
            router = DeliveryRouter(config)

            def fake_say(message: str, *, started_at: float | None = None) -> DeliveryResult:
                self.assertEqual(message, "Test.")
                self.assertIsNotNone(read_voice_activity_started_at(config, now=started_at))
                return DeliveryResult(channel="macos_say", delivered=True, spoken=True)

            router._say = fake_say

            with patch("agent_voice.delivery.router.time.time", return_value=100.0):
                result = router._voice("Test.")

            self.assertTrue(result.delivered)
            self.assertIsNone(read_voice_activity_started_at(config, now=101.0))

    def test_openai_tts_estimates_token_cost_and_records_request_ids(self) -> None:
        class FakeResponse:
            headers = {"x-request-id": "req_123"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return b"fake-audio"

        class FakeProcess:
            pid = 12345

            def wait(self, timeout: float) -> int:
                return 0

        with tempfile.TemporaryDirectory() as tmp:
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                voice_backend="openai_tts",
                voice_name="marin",
                voice_model="gpt-4o-mini-tts",
                voice_instructions="Speak calmly.",
                voice_audio_tokens_per_second=20.0,
                voice_text_input_price_per_million_tokens_usd=0.60,
                voice_audio_output_price_per_million_tokens_usd=12.0,
            )

            with (
                patch(
                    "agent_voice.delivery.router.resolve_openai_api_key",
                    return_value=("test-key", SimpleNamespace(source="env")),
                ),
                patch("agent_voice.delivery.router.shutil.which", return_value="/usr/bin/tool"),
                patch("agent_voice.delivery.router.urllib.request.urlopen", return_value=FakeResponse()),
                patch("agent_voice.delivery.router._audio_file_duration_seconds", return_value=3.0),
                patch("agent_voice.delivery.router.subprocess.Popen", return_value=FakeProcess()),
            ):
                result = DeliveryRouter(config)._openai_tts("Test message.", started_at=0)

            self.assertTrue(result.delivered)
            self.assertEqual(result.audio_request_id, "req_123")
            self.assertIsNotNone(result.audio_client_request_id)
            self.assertGreater(result.audio_input_text_tokens, 0)
            self.assertEqual(result.audio_output_audio_tokens, 60)
            self.assertAlmostEqual(result.audio_output_cost_usd, 0.00072)
            self.assertAlmostEqual(
                result.audio_cost_usd,
                result.audio_input_cost_usd + result.audio_output_cost_usd,
            )


if __name__ == "__main__":
    unittest.main()
