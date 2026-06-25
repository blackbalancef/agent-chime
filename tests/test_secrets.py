import os
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from agent_voice.config import AgentVoiceConfig
from agent_voice.secrets import get_dotenv_secret, resolve_openai_api_key, validate_openai_tts_key


class SecretTests(unittest.TestCase):
    def test_env_key_wins(self) -> None:
        env_name = "VOICCCE_TEST_OPENAI_KEY"
        os.environ[env_name] = "test-key"
        try:
            config = AgentVoiceConfig(
                voice_api_key_env=env_name,
                voice_api_key_keychain_service="voiccce-test-unused",
                voice_api_key_keychain_account="openai-test-unused",
            )

            key, status = resolve_openai_api_key(config)

            self.assertEqual(key, "test-key")
            self.assertEqual(status.source, "env")
            self.assertTrue(status.available)
        finally:
            os.environ.pop(env_name, None)

    def test_dotenv_key_is_used_after_env(self) -> None:
        env_name = "VOICCCE_TEST_OPENAI_KEY"
        os.environ.pop(env_name, None)
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            dotenv_path = Path(tmp) / ".env"
            dotenv_path.write_text(f'{env_name}="dotenv-key"\n', encoding="utf-8")
            config = AgentVoiceConfig(
                config_path=config_path,
                voice_api_key_env=env_name,
                voice_api_key_keychain_service="voiccce-test-unused",
                voice_api_key_keychain_account="openai-test-unused",
            )

            key, status = resolve_openai_api_key(config)

            self.assertEqual(key, "dotenv-key")
            self.assertEqual(status.source, "dotenv")
            self.assertTrue(status.available)

    def test_dotenv_parser_ignores_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dotenv_path = Path(tmp) / ".env"
            dotenv_path.write_text("# ignored\nOPENAI_API_KEY=abc\n", encoding="utf-8")

            self.assertEqual(get_dotenv_secret(dotenv_path, "OPENAI_API_KEY"), "abc")

    def test_validate_openai_tts_key_generates_audio(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return b"audio"

        config = AgentVoiceConfig(voice_model="gpt-4o-mini-tts", voice_format="mp3")

        with patch("agent_voice.secrets.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            result = validate_openai_tts_key(config, "sk-test", voice="coral")

        self.assertTrue(result.ok)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/audio/speech")
        self.assertIn(b'"voice": "coral"', request.data)

    def test_validate_openai_tts_key_reports_http_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.openai.com/v1/audio/speech",
            401,
            "Unauthorized",
            {},
            BytesIO(b'{"error": "bad key"}'),
        )

        with patch("agent_voice.secrets.urllib.request.urlopen", side_effect=error):
            result = validate_openai_tts_key(AgentVoiceConfig(), "sk-bad", voice="marin")
        error.close()

        self.assertFalse(result.ok)
        self.assertIn("HTTP 401", result.error or "")


if __name__ == "__main__":
    unittest.main()
