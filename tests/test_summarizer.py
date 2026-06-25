import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_voice.config import AgentVoiceConfig
from agent_voice.intelligence.summarizer import (
    limit_summary_source_text,
    summarize_notification,
)
from agent_voice.models import SessionStatus
from agent_voice.secrets import SecretStatus


class FakeResponse:
    headers = {"x-request-id": "req_123"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Voice summaries are ready",
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens": 10,
                },
            }
        ).encode("utf-8")


class SummarizerTests(unittest.TestCase):
    def test_limit_summary_source_text_keeps_start_and_end(self) -> None:
        source = "START " + ("middle " * 40) + "END"

        limited = limit_summary_source_text(source, max_chars=80)

        self.assertLessEqual(len(limited), 80)
        self.assertIn("START", limited)
        self.assertIn("END", limited)
        self.assertIn("omitted middle", limited)

    def test_openai_summarizer_extracts_text_and_estimates_cost(self) -> None:
        config = AgentVoiceConfig(
            summary_enabled=True,
            summary_prompt="Say this for {project}: {message}",
            summary_max_input_chars=80,
            summary_max_words=8,
        )
        candidate = SimpleNamespace(
            agent_name="codex",
            project_name="api",
            status=SessionStatus.COMPLETED,
            message="Session api is fully complete.",
            summary_source_text="START " + ("middle " * 40) + "END",
        )
        seen_payload = None

        def fake_urlopen(request, timeout):
            nonlocal seen_payload
            seen_payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(timeout, config.summary_timeout_seconds)
            return FakeResponse()

        with (
            patch(
                "agent_voice.intelligence.summarizer.resolve_openai_api_key",
                return_value=("sk-test", SecretStatus(source="env", available=True)),
            ),
            patch("agent_voice.intelligence.summarizer.urllib.request.urlopen", fake_urlopen),
        ):
            result = summarize_notification(config, candidate)

        self.assertEqual(result.message, "Voice summaries are ready.")
        self.assertEqual(result.raw_text, "Voice summaries are ready")
        self.assertIn("Say this for api:", result.prompt)
        self.assertIn("START", result.prompt)
        self.assertEqual(result.request_id, "req_123")
        self.assertEqual(result.input_text_tokens, 100)
        self.assertEqual(result.cached_input_text_tokens, 20)
        self.assertEqual(result.output_text_tokens, 10)
        self.assertAlmostEqual(result.cost_usd, 0.0000289)
        self.assertEqual(seen_payload["model"], "gpt-5.4-nano")
        self.assertNotIn("max_output_tokens", seen_payload)
        self.assertIn("START", seen_payload["input"])
        self.assertIn("END", seen_payload["input"])
        self.assertIn("omitted middle", seen_payload["input"])
        self.assertLessEqual(len(seen_payload["input"]), 120)


    def test_prompt_forces_configured_language(self) -> None:
        config = AgentVoiceConfig(language="ru", summary_enabled=True)
        candidate = SimpleNamespace(
            agent_name="codex",
            project_name="api",
            status=SessionStatus.COMPLETED,
            message="All tests pass.",
            summary_source_text="All tests pass.",
        )

        with (
            patch(
                "agent_voice.intelligence.summarizer.resolve_openai_api_key",
                return_value=("sk-test", SecretStatus(source="env", available=True)),
            ),
            patch("agent_voice.intelligence.summarizer.urllib.request.urlopen", lambda request, timeout: FakeResponse()),
        ):
            result = summarize_notification(config, candidate)

        self.assertIn("Write the notification in Russian", result.prompt)

    def test_prompt_accepts_custom_language_name(self) -> None:
        config = AgentVoiceConfig(language="Spanish", summary_enabled=True)
        candidate = SimpleNamespace(
            agent_name="codex",
            project_name="api",
            status=SessionStatus.COMPLETED,
            message="All tests pass.",
            summary_source_text="All tests pass.",
        )

        with (
            patch(
                "agent_voice.intelligence.summarizer.resolve_openai_api_key",
                return_value=("sk-test", SecretStatus(source="env", available=True)),
            ),
            patch("agent_voice.intelligence.summarizer.urllib.request.urlopen", lambda request, timeout: FakeResponse()),
        ):
            result = summarize_notification(config, candidate)

        self.assertIn("Write the notification in Spanish", result.prompt)


if __name__ == "__main__":
    unittest.main()
