from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from agent_voice.config import AgentVoiceConfig
from agent_voice.runtime import (
    clear_voice_activity,
    clear_voice_pid,
    is_voice_muted,
    start_voice_activity,
    voice_stop_requested_after,
    write_voice_pid,
)
from agent_voice.secrets import resolve_openai_api_key
from agent_voice.tts_cost import estimate_openai_tts_cost


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    channel: str
    delivered: bool
    spoken: bool = False
    error: str | None = None
    audio_generated: bool = False
    audio_duration_seconds: float = 0.0
    audio_cost_usd: float = 0.0
    audio_request_id: str | None = None
    audio_client_request_id: str | None = None
    audio_input_text_tokens: int = 0
    audio_output_audio_tokens: int = 0
    audio_input_cost_usd: float = 0.0
    audio_output_cost_usd: float = 0.0
    audio_token_count_method: str | None = None


AFINFO_DURATION_RE = re.compile(r"estimated duration:\s*([0-9]+(?:\.[0-9]+)?)\s*sec", re.IGNORECASE)

DEFAULT_TEST_MESSAGE = "Voiccce is working."


def test_message(config: AgentVoiceConfig) -> str:
    """The sample phrase used to check the current voice/speed settings."""
    templates = config.message_templates.get(config.language, {})
    return templates.get("test") or DEFAULT_TEST_MESSAGE


class DeliveryRouter:
    def __init__(self, config: AgentVoiceConfig, *, terminal_only: bool = False) -> None:
        self.config = config
        self.terminal_only = terminal_only

    def deliver(self, message: str) -> list[DeliveryResult]:
        if self.terminal_only:
            return [self._terminal(message)]

        results: list[DeliveryResult] = []
        if self.config.voice_enabled:
            result = self._voice(message)
            results.append(result)
            if result.delivered:
                return results
            if self.config.voice_backend == "openai_tts" and result.channel not in {"voice_muted", "voice_cancelled"}:
                fallback = self._say(message)
                results.append(fallback)
                if fallback.delivered:
                    return results

        if self.config.desktop_enabled:
            result = self._desktop(message)
            results.append(result)
            if result.delivered:
                return results

        if self.config.terminal_enabled:
            results.append(self._terminal(message))

        return results

    def deliver_configured_voice(self, message: str) -> DeliveryResult:
        """Play through the configured voice backend without desktop/terminal fallback."""
        if not self.config.voice_enabled:
            return DeliveryResult(channel="voice_disabled", delivered=False, error="voice disabled")
        return self._voice(message)

    def _voice(self, message: str) -> DeliveryResult:
        if is_voice_muted(self.config):
            return DeliveryResult(channel="voice_muted", delivered=False, error="voice muted")
        started_at = time.time()
        activity_started_at = start_voice_activity(self.config, now=started_at)
        try:
            if self.config.voice_backend == "openai_tts":
                return self._openai_tts(message, started_at=started_at)
            return self._say(message, started_at=started_at)
        finally:
            clear_voice_activity(self.config, activity_started_at)

    def _say(self, message: str, *, started_at: float | None = None) -> DeliveryResult:
        if shutil.which("say") is None:
            return DeliveryResult(channel="macos_say", delivered=False, error="say command not found")
        if started_at is not None and voice_stop_requested_after(self.config, started_at):
            return DeliveryResult(channel="voice_cancelled", delivered=False, error="voice cancelled")
        cmd = ["say", "-r", str(self.config.voice_rate)]
        if self.config.voice_name:
            cmd.extend(["-v", self.config.voice_name])
        cmd.append(message)
        try:
            process = subprocess.Popen(cmd, start_new_session=True)
            write_voice_pid(self.config, process.pid)
            playback_started_at = time.monotonic()
            return_code = process.wait(timeout=15)
            audio_duration_seconds = max(0.0, time.monotonic() - playback_started_at)
            if return_code != 0:
                if voice_stop_requested_after(self.config, started_at or 0):
                    return DeliveryResult(
                        channel="voice_cancelled",
                        delivered=False,
                        error="voice cancelled",
                        audio_duration_seconds=audio_duration_seconds,
                    )
                return DeliveryResult(
                    channel="macos_say",
                    delivered=False,
                    error=f"say exited with {return_code}",
                    audio_duration_seconds=audio_duration_seconds,
                )
            return DeliveryResult(
                channel="macos_say",
                delivered=True,
                spoken=True,
                audio_generated=True,
                audio_duration_seconds=audio_duration_seconds,
            )
        except Exception as exc:  # pragma: no cover - platform dependent
            return DeliveryResult(channel="macos_say", delivered=False, error=str(exc))
        finally:
            if "process" in locals():
                clear_voice_pid(self.config, process.pid)

    def _openai_tts(self, message: str, *, started_at: float) -> DeliveryResult:
        api_key, secret_status = resolve_openai_api_key(self.config)
        if not api_key:
            return DeliveryResult(
                channel="openai_tts",
                delivered=False,
                error=(
                    f"{self.config.voice_api_key_env} is not set and "
                    f"Keychain secret {self.config.voice_api_key_keychain_service}/"
                    f"{self.config.voice_api_key_keychain_account} is missing"
                ),
            )
        if shutil.which("afplay") is None:
            return DeliveryResult(channel="openai_tts", delivered=False, error="afplay command not found")

        suffix = f".{self.config.voice_format}"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            tmp_path = Path(tmp_file.name)
        payload = {
            "model": self.config.voice_model,
            "voice": self.config.voice_name or "marin",
            "input": message,
            "response_format": self.config.voice_format,
            "speed": self.config.voice_speed,
        }
        if self.config.voice_instructions:
            payload["instructions"] = self.config.voice_instructions

        client_request_id = uuid.uuid4().hex
        request = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Agent-Voice-Key-Source": secret_status.source,
                "X-Client-Request-Id": client_request_id,
            },
            method="POST",
        )
        try:
            request_id = None
            with urllib.request.urlopen(request, timeout=self.config.voice_timeout_seconds) as response:
                request_id = response.headers.get("x-request-id")
                tmp_path.write_bytes(response.read())
            file_duration_seconds = _audio_file_duration_seconds(tmp_path)
            audio_duration_seconds = file_duration_seconds or 0.0
            if voice_stop_requested_after(self.config, started_at):
                return self._openai_tts_delivery_result(
                    channel="voice_cancelled",
                    delivered=False,
                    error="voice cancelled",
                    message=message,
                    audio_duration_seconds=audio_duration_seconds,
                    request_id=request_id,
                    client_request_id=client_request_id,
                )
            process = subprocess.Popen(["afplay", str(tmp_path)], start_new_session=True)
            write_voice_pid(self.config, process.pid)
            playback_started_at = time.monotonic()
            return_code = process.wait(timeout=self.config.voice_timeout_seconds + 20)
            playback_duration_seconds = max(0.0, time.monotonic() - playback_started_at)
            if audio_duration_seconds <= 0:
                audio_duration_seconds = playback_duration_seconds
            if return_code != 0:
                if voice_stop_requested_after(self.config, started_at):
                    return self._openai_tts_delivery_result(
                        channel="voice_cancelled",
                        delivered=False,
                        error="voice cancelled",
                        message=message,
                        audio_duration_seconds=audio_duration_seconds,
                        request_id=request_id,
                        client_request_id=client_request_id,
                    )
                return self._openai_tts_delivery_result(
                    channel="openai_tts",
                    delivered=False,
                    error=f"afplay exited with {return_code}",
                    message=message,
                    audio_duration_seconds=audio_duration_seconds,
                    request_id=request_id,
                    client_request_id=client_request_id,
                )
            return self._openai_tts_delivery_result(
                channel="openai_tts",
                delivered=True,
                spoken=True,
                message=message,
                audio_duration_seconds=audio_duration_seconds,
                request_id=request_id,
                client_request_id=client_request_id,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return DeliveryResult(channel="openai_tts", delivered=False, error=f"HTTP {exc.code}: {body[:300]}")
        except Exception as exc:  # pragma: no cover - network/platform dependent
            return DeliveryResult(channel="openai_tts", delivered=False, error=str(exc))
        finally:
            if "process" in locals():
                clear_voice_pid(self.config, process.pid)
            tmp_path.unlink(missing_ok=True)

    def _openai_tts_delivery_result(
        self,
        *,
        channel: str,
        delivered: bool,
        message: str,
        audio_duration_seconds: float,
        request_id: str | None,
        client_request_id: str,
        spoken: bool = False,
        error: str | None = None,
    ) -> DeliveryResult:
        estimate = estimate_openai_tts_cost(
            input_text=message,
            instructions=self.config.voice_instructions,
            duration_seconds=audio_duration_seconds,
            model=self.config.voice_model,
            text_input_price_per_million_tokens_usd=self.config.voice_text_input_price_per_million_tokens_usd,
            audio_output_price_per_million_tokens_usd=self.config.voice_audio_output_price_per_million_tokens_usd,
            audio_tokens_per_second=self.config.voice_audio_tokens_per_second,
        )
        return DeliveryResult(
            channel=channel,
            delivered=delivered,
            spoken=spoken,
            error=error,
            audio_generated=True,
            audio_duration_seconds=audio_duration_seconds,
            audio_cost_usd=estimate.total_cost_usd,
            audio_request_id=request_id,
            audio_client_request_id=client_request_id,
            audio_input_text_tokens=estimate.input_text_tokens,
            audio_output_audio_tokens=estimate.output_audio_tokens,
            audio_input_cost_usd=estimate.input_cost_usd,
            audio_output_cost_usd=estimate.output_cost_usd,
            audio_token_count_method=estimate.token_count_method,
        )

    def _desktop(self, message: str) -> DeliveryResult:
        if shutil.which("osascript") is None:
            return DeliveryResult(channel="macos_notification", delivered=False, error="osascript not found")
        script = f'display notification "{_escape_applescript(message)}" with title "Voiccce"'
        try:
            subprocess.run(["osascript", "-e", script], check=True, timeout=5)
            return DeliveryResult(channel="macos_notification", delivered=True)
        except Exception as exc:  # pragma: no cover - platform dependent
            return DeliveryResult(channel="macos_notification", delivered=False, error=str(exc))

    def _terminal(self, message: str) -> DeliveryResult:
        print(message, file=sys.stderr)
        return DeliveryResult(channel="terminal_log", delivered=True)


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _audio_file_duration_seconds(path: Path) -> float | None:
    if shutil.which("afinfo") is None:
        return None
    try:
        result = subprocess.run(
            ["afinfo", str(path)],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = AFINFO_DURATION_RE.search(result.stdout)
    if not match:
        return None
    try:
        return max(0.0, float(match.group(1)))
    except ValueError:
        return None
