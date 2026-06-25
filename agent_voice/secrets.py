from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import AgentVoiceConfig


@dataclass(frozen=True, slots=True)
class SecretStatus:
    source: str
    available: bool


@dataclass(frozen=True, slots=True)
class OpenAIKeyValidation:
    ok: bool
    error: str | None = None


def resolve_openai_api_key(config: AgentVoiceConfig) -> tuple[str | None, SecretStatus]:
    env_value = os.environ.get(config.voice_api_key_env)
    if env_value:
        return env_value, SecretStatus(source="env", available=True)

    dotenv_value = get_dotenv_secret(config.config_path.parent / ".env", config.voice_api_key_env)
    if dotenv_value:
        return dotenv_value, SecretStatus(source="dotenv", available=True)

    keychain_value = get_keychain_secret(
        service=config.voice_api_key_keychain_service,
        account=config.voice_api_key_keychain_account,
    )
    if keychain_value:
        return keychain_value, SecretStatus(source="keychain", available=True)

    return None, SecretStatus(source="missing", available=False)


def validate_openai_tts_key(
    config: AgentVoiceConfig,
    api_key: str,
    *,
    voice: str | None = None,
    model: str | None = None,
) -> OpenAIKeyValidation:
    """Validate an OpenAI key by generating a tiny TTS response without playback."""
    payload = {
        "model": model or config.voice_model,
        "voice": voice or config.voice_name or "marin",
        "input": "Voiccce key check.",
        "response_format": config.voice_format,
        "speed": 1.0,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Client-Request-Id": uuid.uuid4().hex,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.voice_timeout_seconds) as response:
            response.read()
        return OpenAIKeyValidation(ok=True)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return OpenAIKeyValidation(ok=False, error=f"HTTP {exc.code}: {body[:300]}")
    except Exception as exc:
        return OpenAIKeyValidation(ok=False, error=str(exc))


def get_dotenv_secret(path: Path, key: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() != key:
            continue
        parsed = _parse_dotenv_value(value.strip())
        return parsed or None
    return None


def _parse_dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def get_openai_secret_status(config: AgentVoiceConfig) -> SecretStatus:
    _, status = resolve_openai_api_key(config)
    return status


def set_openai_keychain_secret(config: AgentVoiceConfig, secret: str) -> None:
    set_keychain_secret(
        service=config.voice_api_key_keychain_service,
        account=config.voice_api_key_keychain_account,
        secret=secret,
    )


def delete_openai_keychain_secret(config: AgentVoiceConfig) -> bool:
    return delete_keychain_secret(
        service=config.voice_api_key_keychain_service,
        account=config.voice_api_key_keychain_account,
    )


def get_keychain_secret(*, service: str, account: str) -> str | None:
    if shutil.which("security") is None:
        return None
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def set_keychain_secret(*, service: str, account: str, secret: str) -> None:
    if shutil.which("security") is None:
        raise RuntimeError("macOS security command not found")
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                service,
                "-a",
                account,
                "-w",
                secret,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or str(exc)) from exc


def delete_keychain_secret(*, service: str, account: str) -> bool:
    if shutil.which("security") is None:
        return False
    result = subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
