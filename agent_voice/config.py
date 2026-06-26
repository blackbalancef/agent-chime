from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .hotkey import DEFAULT_STOP_SPEAKING_HOTKEY, parse_hotkey
from .tts_cost import (
    DEFAULT_TTS_AUDIO_OUTPUT_PRICE_PER_MILLION_TOKENS_USD,
    DEFAULT_TTS_AUDIO_TOKENS_PER_SECOND,
    DEFAULT_TTS_ESTIMATED_COST_PER_MINUTE_USD,
    DEFAULT_TTS_TEXT_INPUT_PRICE_PER_MILLION_TOKENS_USD,
    audio_tokens_per_second_from_minute_cost,
)


DEFAULT_HOME = Path.home() / ".voiccce"
DEFAULT_CONFIG_PATH = DEFAULT_HOME / "config.toml"
DEFAULT_DB_PATH = DEFAULT_HOME / "events.sqlite3"
# Bumped whenever the on-disk config layout gains keys/sections that older files
# lack. ``load_config`` back-fills missing defaults idempotently and stamps
# ``[meta].schema_version`` so future migrations can branch on the prior value.
CONFIG_SCHEMA_VERSION = 1


class ConfigError(Exception):
    """Raised when a config file cannot be parsed or written safely.

    Carries the offending ``path`` and, when the underlying
    :class:`tomllib.TOMLDecodeError` exposes it, the 1-based ``line`` number, plus
    a human-readable ``hint`` describing how to recover.
    """

    def __init__(
        self,
        message: str,
        *,
        path: Path | None = None,
        line: int | None = None,
        hint: str | None = None,
    ) -> None:
        self.path = path
        self.line = line
        self.hint = hint or (
            "Fix the TOML syntax, or restore the most recent "
            "config.toml.bak-* backup written alongside it."
        )
        location = ""
        if path is not None:
            location = f" in {path}"
            if line is not None:
                location += f" (line {line})"
        super().__init__(f"{message}{location}. {self.hint}")
# Languages with bundled template-only messages. AI summary output can target
# any non-empty language name through [user].language.
SUPPORTED_LANGUAGES = ("en", "ru")
# Human-readable language names injected into the summary prompt so the model is
# instructed to write in the configured language ("Russian") rather than a code.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ru": "Russian",
}
SUMMARY_PRIVACY_LEVELS = ("metadata_only", "full_last_message")
SUMMARY_PROVIDERS = ("fallback", "openai")
DEFAULT_SUMMARY_MODEL = "gpt-5.4-nano"
# Curated lists shown in the menu bar model pickers. Edit to taste; the current
# value is always offered even if it is not listed here.
SUMMARY_MODEL_CHOICES = ("gpt-5.4-nano", "gpt-5.4-mini", "gpt-4o-mini")
TTS_MODEL_CHOICES = ("gpt-4o-mini-tts", "tts-1", "tts-1-hd")
# Voices offered in the menu bar picker, per backend. The current value is
# always offered even if it is not in this list.
VOICE_CHOICES: dict[str, tuple[str, ...]] = {
    "openai_tts": ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"),
    "macos_say": ("Alex", "Samantha", "Daniel", "Karen", "Moira", "Tessa", "Fred"),
}
# Approximate prompt-cache warm window per agent, in minutes. Used only to phrase
# idle reminders ("reply while the cache is still warm"); not an exact guarantee.
# Claude's ephemeral prompt cache defaults to ~5 min; OpenAI/Codex prefix cache
# typically survives ~5-10 min of inactivity.
CACHE_WARM_MINUTES: dict[str, int] = {"claude-code": 5, "codex": 10}
DEFAULT_CACHE_WARM_MINUTES = 5


def cache_warm_minutes(agent_name: str | None) -> int:
    return CACHE_WARM_MINUTES.get((agent_name or "").lower(), DEFAULT_CACHE_WARM_MINUTES)
DEFAULT_SUMMARY_TEXT_INPUT_PRICE_PER_MILLION_TOKENS_USD = 0.20
DEFAULT_SUMMARY_CACHED_INPUT_PRICE_PER_MILLION_TOKENS_USD = 0.02
DEFAULT_SUMMARY_TEXT_OUTPUT_PRICE_PER_MILLION_TOKENS_USD = 1.25
DEFAULT_SUMMARY_MAX_INPUT_CHARS = 6000
DEFAULT_SUMMARY_PROMPT = """Rewrite the final assistant update into one natural spoken notification.
Write the notification in {language_name}, regardless of the language of the update below — translate it if needed.
Project: {project}
Agent: {agent}
Status: {status}

Always start the notification with the project name ({project}) so it is immediately clear which project this is about.
Keep only what the user needs to know now. Write no more than {max_words} words, and always finish your sentence completely — never stop mid-thought or mid-word.
Sound natural and varied, not like a status template. Do not mention internal paths, commands, or tests unless they are essential.
Return only the text to speak.

Final assistant update:
{message}
"""

# Older default prompts that predate a wording change. An existing config whose
# [summary].prompt still equals one of these (verbatim) is upgraded to the current
# DEFAULT_SUMMARY_PROMPT on load; a user's customized prompt is never touched.
_STALE_SUMMARY_PROMPTS: tuple[str, ...] = (
    """Rewrite the final assistant update into one natural spoken notification.
Write the notification in {language_name}, regardless of the language of the update below — translate it if needed.
Project: {project}
Agent: {agent}
Status: {status}

Keep only what the user needs to know now. Write no more than {max_words} words, and always finish your sentence completely — never stop mid-thought or mid-word.
Sound natural and varied, not like a status template. Do not mention internal paths, commands, or tests unless they are essential.
Return only the text to speak.

Final assistant update:
{message}
""",
)
DEFAULT_SUMMARY_CONFIG: dict[str, str | int | float | bool] = {
    "enabled": True,
    "provider": "openai",
    "model": DEFAULT_SUMMARY_MODEL,
    "privacy_level": "full_last_message",
    "max_input_chars": DEFAULT_SUMMARY_MAX_INPUT_CHARS,
    "max_words": 40,
    "timeout_seconds": 5,
    "text_input_price_per_million_tokens_usd": DEFAULT_SUMMARY_TEXT_INPUT_PRICE_PER_MILLION_TOKENS_USD,
    "cached_input_price_per_million_tokens_usd": DEFAULT_SUMMARY_CACHED_INPUT_PRICE_PER_MILLION_TOKENS_USD,
    "text_output_price_per_million_tokens_usd": DEFAULT_SUMMARY_TEXT_OUTPUT_PRICE_PER_MILLION_TOKENS_USD,
    "pipeline_log": True,
    "prompt": DEFAULT_SUMMARY_PROMPT,
}
DEFAULT_HOTKEY_CONFIG: dict[str, str | bool] = {
    "enabled": True,
    "stop_speaking": DEFAULT_STOP_SPEAKING_HOTKEY,
}
DEFAULT_DAEMON_CONFIG: dict[str, str | int | float | bool] = {
    "poll_interval_ms": 500,
    "event_retention_days": 30,
    "max_log_bytes": 5_000_000,
}
DEFAULT_LIMITS_CONFIG: dict[str, str | int | float | bool] = {
    "min_seconds_between_voice_messages": 8,
    "grouping_window_seconds": 20,
    "max_events_per_minute": 6,
    "duplicate_cooldown_seconds": 300,
    "daily_spend_cap_usd": 0.0,
    "monthly_spend_cap_usd": 0.0,
}
DEFAULT_EVENTS_CONFIG: dict[str, str | int | float | bool] = {
    "task_finished": True,
    "permission_needed": True,
    "input_needed": True,
    "task_failed": True,
    "subagent_finished": False,
}
DEFAULT_QUIET_HOURS_CONFIG: dict[str, str | int | float | bool] = {
    "enabled": True,
    "from": "23:00",
    "to": "09:00",
    "voice": False,
    "desktop": True,
}
DEFAULT_AUTOSTART_CONFIG: dict[str, str | int | float | bool] = {
    "managed": False,
}
DEFAULT_REMINDERS_CONFIG: dict[str, str | int | float | bool] = {
    "enabled": True,
    # Speak a short "still waiting" nudge this many minutes BEFORE the agent's
    # prompt cache is expected to expire, so a reply still lands on a warm cache.
    "margin_minutes": 1,
}


DEFAULT_CONFIG_TOML_TEMPLATE = """[meta]
schema_version = {schema_version}

[user]
language = "en"
timezone = "Europe/Belgrade"

[daemon]
database_path = "{database_path}"
poll_interval_ms = 500
event_retention_days = 30
max_log_bytes = 5000000

[summary]
enabled = true
provider = "openai"
model = "gpt-5.4-nano"
privacy_level = "full_last_message"
max_input_chars = 6000
max_words = 40
timeout_seconds = 5
text_input_price_per_million_tokens_usd = 0.20
cached_input_price_per_million_tokens_usd = 0.02
text_output_price_per_million_tokens_usd = 1.25
pipeline_log = true
prompt = '''
Rewrite the final assistant update into one natural spoken notification.
Write the notification in {language_name}, regardless of the language of the update below — translate it if needed.
Project: {project}
Agent: {agent}
Status: {status}

Always start the notification with the project name ({project}) so it is immediately clear which project this is about.
Keep only what the user needs to know now. Write no more than {max_words} words, and always finish your sentence completely — never stop mid-thought or mid-word.
Sound natural and varied, not like a status template. Do not mention internal paths, commands, or tests unless they are essential.
Return only the text to speak.

Final assistant update:
{message}
'''

[messages.en]
failed = "{agent} in {project} failed{reason_clause}."
permission_needed = "{agent} in {project} needs permission{reason_clause}."
attention_required = "{agent} in {project} needs attention{reason_clause}."
completed = "Session {project} is fully complete."
completed_with_summary = "Session {project} is fully complete. Summary: {summary}."
handled = "Event in {project} was handled."
test = "Voiccce is working."
idle_reminder = "{project} is waiting for your reply."
grouped_prefix = "Updates: {items}."
grouped_many = "{count} sessions: {summary}."
grouped_failed_fragment = "{project} failed"
grouped_attention_fragment = "{project} needs attention"
grouped_completed_fragment = "{project} completed"
grouped_attention_count = "{count} need attention"
grouped_failed_count = "{count} failed"
grouped_completed_count = "{count} completed"
grouped_updates_count = "{count} updates"

[messages.ru]
failed = "{agent} в проекте {project} завершился с ошибкой{reason_clause}."
permission_needed = "{agent} в проекте {project} запрашивает разрешение{reason_clause}."
attention_required = "{agent} в проекте {project} требует внимания{reason_clause}."
completed = "Сессия {project} полностью завершена."
completed_with_summary = "Сессия {project} полностью завершена. Итог: {summary}."
handled = "Событие в проекте {project} обработано."
test = "Voiccce работает."
idle_reminder = "{project} ждёт твоего ответа."
grouped_prefix = "Обновления: {items}."
grouped_many = "Сессий: {count}. {summary}."
grouped_failed_fragment = "{project} — ошибка"
grouped_attention_fragment = "{project} требует внимания"
grouped_completed_fragment = "{project} завершён"
grouped_attention_count = "{count} требуют внимания"
grouped_failed_count = "{count} с ошибкой"
grouped_completed_count = "{count} завершено"
grouped_updates_count = "{count} обновлений"

[voice]
enabled = true
backend = "macos_say"
voice = "Alex"
rate = 185
speed = 1.0
model = "gpt-4o-mini-tts"
format = "mp3"
estimated_cost_per_minute_usd = 0.015
text_input_price_per_million_tokens_usd = 0.60
audio_output_price_per_million_tokens_usd = 12.00
audio_tokens_per_second = 20.833333
api_key_env = "OPENAI_API_KEY"
api_key_keychain_service = "voiccce"
api_key_keychain_account = "openai"
instructions = "Speak naturally, calmly, and briefly. This is a short developer notification."
timeout_seconds = 15
interrupt_on_user_input = true

[hotkey]
# Global keyboard shortcut (works in any app while the menu bar app runs) that
# instantly stops the current voice playback — handy during meetings. Modifiers:
# cmd, ctrl, alt/option, shift. Set enabled = false to turn it off.
enabled = true
stop_speaking = "alt+cmd+s"

[desktop]
enabled = true

[terminal]
enabled = true

[events]
task_finished = true
permission_needed = true
input_needed = true
task_failed = true
subagent_finished = false

[limits]
min_seconds_between_voice_messages = 8
grouping_window_seconds = 20
max_events_per_minute = 6
duplicate_cooldown_seconds = 300
daily_spend_cap_usd = 0.0
monthly_spend_cap_usd = 0.0

[quiet_hours]
enabled = true
from = "23:00"
to = "09:00"
voice = false
desktop = true

[autostart]
managed = false

[reminders]
enabled = true
margin_minutes = 1
"""

DEFAULT_MESSAGE_TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "failed": "{agent} in {project} failed{reason_clause}.",
        "permission_needed": "{agent} in {project} needs permission{reason_clause}.",
        "attention_required": "{agent} in {project} needs attention{reason_clause}.",
        "completed": "Session {project} is fully complete.",
        "completed_with_summary": "Session {project} is fully complete. Summary: {summary}.",
        "handled": "Event in {project} was handled.",
        "test": "Voiccce is working.",
        "idle_reminder": "{project} is waiting for your reply.",
        "grouped_prefix": "Updates: {items}.",
        "grouped_many": "{count} sessions: {summary}.",
        "grouped_failed_fragment": "{project} failed",
        "grouped_attention_fragment": "{project} needs attention",
        "grouped_completed_fragment": "{project} completed",
        "grouped_attention_count": "{count} need attention",
        "grouped_failed_count": "{count} failed",
        "grouped_completed_count": "{count} completed",
        "grouped_updates_count": "{count} updates",
    },
    "ru": {
        "failed": "{agent} в проекте {project} завершился с ошибкой{reason_clause}.",
        "permission_needed": "{agent} в проекте {project} запрашивает разрешение{reason_clause}.",
        "attention_required": "{agent} в проекте {project} требует внимания{reason_clause}.",
        "completed": "Сессия {project} полностью завершена.",
        "completed_with_summary": "Сессия {project} полностью завершена. Итог: {summary}.",
        "handled": "Событие в проекте {project} обработано.",
        "test": "Voiccce работает.",
        "idle_reminder": "{project} ждёт твоего ответа.",
        "grouped_prefix": "Обновления: {items}.",
        "grouped_many": "Сессий: {count}. {summary}.",
        "grouped_failed_fragment": "{project} — ошибка",
        "grouped_attention_fragment": "{project} требует внимания",
        "grouped_completed_fragment": "{project} завершён",
        "grouped_attention_count": "{count} требуют внимания",
        "grouped_failed_count": "{count} с ошибкой",
        "grouped_completed_count": "{count} завершено",
        "grouped_updates_count": "{count} обновлений",
    },
}


@dataclass(slots=True)
class AgentVoiceConfig:
    config_path: Path = DEFAULT_CONFIG_PATH
    database_path: Path = DEFAULT_DB_PATH
    language: str = "en"
    timezone: str = "Europe/Belgrade"
    poll_interval_ms: int = 500
    voice_enabled: bool = True
    voice_backend: str = "macos_say"
    voice_name: str = "Alex"
    voice_rate: int = 185
    voice_speed: float = 1.0
    voice_model: str = "gpt-4o-mini-tts"
    voice_format: str = "mp3"
    voice_estimated_cost_per_minute_usd: float = DEFAULT_TTS_ESTIMATED_COST_PER_MINUTE_USD
    voice_text_input_price_per_million_tokens_usd: float = DEFAULT_TTS_TEXT_INPUT_PRICE_PER_MILLION_TOKENS_USD
    voice_audio_output_price_per_million_tokens_usd: float = DEFAULT_TTS_AUDIO_OUTPUT_PRICE_PER_MILLION_TOKENS_USD
    voice_audio_tokens_per_second: float = DEFAULT_TTS_AUDIO_TOKENS_PER_SECOND
    voice_api_key_env: str = "OPENAI_API_KEY"
    voice_api_key_keychain_service: str = "voiccce"
    voice_api_key_keychain_account: str = "openai"
    voice_instructions: str = "Speak naturally, calmly, and briefly. This is a short developer notification."
    voice_timeout_seconds: int = 15
    voice_interrupt_on_user_input: bool = True
    hotkey_enabled: bool = True
    hotkey_stop_speaking: str = DEFAULT_STOP_SPEAKING_HOTKEY
    summary_enabled: bool = True
    summary_provider: str = "openai"
    summary_model: str = DEFAULT_SUMMARY_MODEL
    summary_privacy_level: str = "full_last_message"
    summary_max_input_chars: int = DEFAULT_SUMMARY_MAX_INPUT_CHARS
    summary_max_words: int = 40
    summary_timeout_seconds: int = 5
    summary_prompt: str = DEFAULT_SUMMARY_PROMPT
    summary_text_input_price_per_million_tokens_usd: float = DEFAULT_SUMMARY_TEXT_INPUT_PRICE_PER_MILLION_TOKENS_USD
    summary_cached_input_price_per_million_tokens_usd: float = DEFAULT_SUMMARY_CACHED_INPUT_PRICE_PER_MILLION_TOKENS_USD
    summary_text_output_price_per_million_tokens_usd: float = DEFAULT_SUMMARY_TEXT_OUTPUT_PRICE_PER_MILLION_TOKENS_USD
    summary_pipeline_log: bool = True
    message_templates: dict[str, dict[str, str]] = field(default_factory=lambda: copy_message_templates(DEFAULT_MESSAGE_TEMPLATES))
    desktop_enabled: bool = True
    terminal_enabled: bool = True
    notify_task_finished: bool = True
    notify_permission_needed: bool = True
    notify_input_needed: bool = True
    notify_task_failed: bool = True
    notify_subagent_finished: bool = False
    min_seconds_between_voice_messages: int = 8
    grouping_window_seconds: int = 20
    max_events_per_minute: int = 6
    duplicate_cooldown_seconds: int = 300
    daily_spend_cap_usd: float = 0.0
    monthly_spend_cap_usd: float = 0.0
    event_retention_days: int = 30
    max_log_bytes: int = 5_000_000
    quiet_hours_enabled: bool = True
    quiet_hours_from: str = "23:00"
    quiet_hours_to: str = "09:00"
    quiet_hours_voice: bool = False
    quiet_hours_desktop: bool = True
    autostart_managed: bool = False
    idle_reminder_enabled: bool = True
    idle_reminder_margin_minutes: int = 1


def expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def _read_config_text(path: Path) -> tuple[str, dict]:
    """Single choke point for reading + parsing a config file.

    Returns the raw text alongside the parsed mapping. Any malformed TOML is
    surfaced as :class:`ConfigError` (carrying the path and, when the parser
    exposes it, the offending line) instead of a raw traceback.
    """
    text = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Malformed TOML: {getattr(exc, 'msg', None) or exc}",
            path=path,
            line=getattr(exc, "lineno", None),
        ) from exc
    return text, data


def load_config(path: str | os.PathLike[str] | None = None) -> AgentVoiceConfig:
    config_path = expand_path(path or DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        return AgentVoiceConfig(
            config_path=config_path,
            database_path=default_database_path_for_config(config_path),
        )

    _, data = _read_config_text(config_path)
    data = _migrate_config(config_path, data)
    daemon = data.get("daemon", {})
    user = data.get("user", {})
    voice = data.get("voice", {})
    summary = data.get("summary", {})
    desktop = data.get("desktop", {})
    terminal = data.get("terminal", {})
    limits = data.get("limits", {})
    events = data.get("events", {})
    hotkey = data.get("hotkey", {})
    quiet_hours = data.get("quiet_hours", {})
    autostart = data.get("autostart", {})
    reminders = data.get("reminders", {})

    voice_estimated_cost_per_minute_usd = float(
        voice.get("estimated_cost_per_minute_usd", DEFAULT_TTS_ESTIMATED_COST_PER_MINUTE_USD)
    )
    voice_audio_output_price_per_million_tokens_usd = float(
        voice.get(
            "audio_output_price_per_million_tokens_usd",
            DEFAULT_TTS_AUDIO_OUTPUT_PRICE_PER_MILLION_TOKENS_USD,
        )
    )
    configured_audio_tokens_per_second = voice.get("audio_tokens_per_second")
    if configured_audio_tokens_per_second is None:
        voice_audio_tokens_per_second = audio_tokens_per_second_from_minute_cost(
            voice_estimated_cost_per_minute_usd,
            voice_audio_output_price_per_million_tokens_usd,
        )
    else:
        voice_audio_tokens_per_second = float(configured_audio_tokens_per_second)

    # A TOML-parseable file can still carry semantically out-of-range values (an
    # invalid clock time, an unknown privacy level). Surface those as a
    # ``ConfigError`` carrying the path so the caller keeps a single error
    # contract instead of leaking a raw ``ValueError`` traceback.
    try:
        summary_privacy_level = normalize_summary_privacy_level(
            summary.get("privacy_level", "full_last_message")
        )
        quiet_hours_from = normalize_hhmm(quiet_hours.get("from", "23:00"))
        quiet_hours_to = normalize_hhmm(quiet_hours.get("to", "09:00"))
    except ValueError as exc:
        raise ConfigError(
            f"Invalid config value: {exc}",
            path=config_path,
            hint="Fix the offending value, or restore the most recent "
            "config.toml.bak-* backup written alongside it.",
        ) from exc

    return AgentVoiceConfig(
        config_path=config_path,
        database_path=expand_path(daemon.get("database_path", str(DEFAULT_DB_PATH))),
        language=normalize_language(user.get("language", "en")),
        timezone=str(user.get("timezone", "Europe/Belgrade")),
        poll_interval_ms=int(daemon.get("poll_interval_ms", 500)),
        voice_enabled=bool(voice.get("enabled", True)),
        voice_backend=voice.get("backend", "macos_say"),
        voice_name=voice.get("voice", "Alex"),
        voice_rate=int(voice.get("rate", 185)),
        voice_speed=float(voice.get("speed", 1.0)),
        voice_model=voice.get("model", "gpt-4o-mini-tts"),
        voice_format=voice.get("format", "mp3"),
        voice_estimated_cost_per_minute_usd=voice_estimated_cost_per_minute_usd,
        voice_text_input_price_per_million_tokens_usd=float(
            voice.get(
                "text_input_price_per_million_tokens_usd",
                DEFAULT_TTS_TEXT_INPUT_PRICE_PER_MILLION_TOKENS_USD,
            )
        ),
        voice_audio_output_price_per_million_tokens_usd=voice_audio_output_price_per_million_tokens_usd,
        voice_audio_tokens_per_second=voice_audio_tokens_per_second,
        voice_api_key_env=voice.get("api_key_env", "OPENAI_API_KEY"),
        voice_api_key_keychain_service=voice.get("api_key_keychain_service", "voiccce"),
        voice_api_key_keychain_account=voice.get("api_key_keychain_account", "openai"),
        voice_instructions=voice.get(
            "instructions",
            "Speak naturally, calmly, and briefly. This is a short developer notification.",
        ),
        voice_timeout_seconds=int(voice.get("timeout_seconds", 15)),
        voice_interrupt_on_user_input=bool(voice.get("interrupt_on_user_input", True)),
        hotkey_enabled=bool(hotkey.get("enabled", True)),
        hotkey_stop_speaking=str(hotkey.get("stop_speaking", DEFAULT_STOP_SPEAKING_HOTKEY)),
        summary_enabled=bool(summary.get("enabled", True)),
        summary_provider=normalize_summary_provider(summary.get("provider", "openai")),
        summary_model=summary.get("model", DEFAULT_SUMMARY_MODEL),
        summary_privacy_level=summary_privacy_level,
        summary_max_input_chars=max(200, int(summary.get("max_input_chars", DEFAULT_SUMMARY_MAX_INPUT_CHARS))),
        summary_max_words=max(1, int(summary.get("max_words", 40))),
        summary_timeout_seconds=max(1, int(summary.get("timeout_seconds", 5))),
        summary_prompt=summary.get("prompt", DEFAULT_SUMMARY_PROMPT),
        summary_text_input_price_per_million_tokens_usd=float(
            summary.get(
                "text_input_price_per_million_tokens_usd",
                DEFAULT_SUMMARY_TEXT_INPUT_PRICE_PER_MILLION_TOKENS_USD,
            )
        ),
        summary_cached_input_price_per_million_tokens_usd=float(
            summary.get(
                "cached_input_price_per_million_tokens_usd",
                DEFAULT_SUMMARY_CACHED_INPUT_PRICE_PER_MILLION_TOKENS_USD,
            )
        ),
        summary_text_output_price_per_million_tokens_usd=float(
            summary.get(
                "text_output_price_per_million_tokens_usd",
                DEFAULT_SUMMARY_TEXT_OUTPUT_PRICE_PER_MILLION_TOKENS_USD,
            )
        ),
        summary_pipeline_log=bool(summary.get("pipeline_log", True)),
        message_templates=load_message_templates(data.get("messages", {})),
        desktop_enabled=bool(desktop.get("enabled", True)),
        terminal_enabled=bool(terminal.get("enabled", True)),
        notify_task_finished=bool(events.get("task_finished", True)),
        notify_permission_needed=bool(events.get("permission_needed", True)),
        notify_input_needed=bool(events.get("input_needed", True)),
        notify_task_failed=bool(events.get("task_failed", True)),
        notify_subagent_finished=bool(events.get("subagent_finished", False)),
        min_seconds_between_voice_messages=int(limits.get("min_seconds_between_voice_messages", 8)),
        grouping_window_seconds=int(limits.get("grouping_window_seconds", 20)),
        max_events_per_minute=int(limits.get("max_events_per_minute", 6)),
        duplicate_cooldown_seconds=int(limits.get("duplicate_cooldown_seconds", 300)),
        daily_spend_cap_usd=max(0.0, float(limits.get("daily_spend_cap_usd", 0.0))),
        monthly_spend_cap_usd=max(0.0, float(limits.get("monthly_spend_cap_usd", 0.0))),
        event_retention_days=max(0, int(daemon.get("event_retention_days", 30))),
        max_log_bytes=max(0, int(daemon.get("max_log_bytes", 5_000_000))),
        quiet_hours_enabled=bool(quiet_hours.get("enabled", True)),
        quiet_hours_from=quiet_hours_from,
        quiet_hours_to=quiet_hours_to,
        quiet_hours_voice=bool(quiet_hours.get("voice", False)),
        quiet_hours_desktop=bool(quiet_hours.get("desktop", True)),
        autostart_managed=bool(autostart.get("managed", False)),
        idle_reminder_enabled=bool(reminders.get("enabled", True)),
        idle_reminder_margin_minutes=max(0, int(reminders.get("margin_minutes", 1))),
    )


def render_default_config_toml(config_path: Path) -> str:
    """Render the bundled template with the per-config database path substituted."""
    database_path = default_database_path_for_config(config_path)
    return (
        DEFAULT_CONFIG_TOML_TEMPLATE
        .replace("{schema_version}", str(CONFIG_SCHEMA_VERSION))
        .replace("{database_path}", str(database_path))
    )


def write_default_config(path: str | os.PathLike[str] | None = None) -> Path:
    config_path = expand_path(path or DEFAULT_CONFIG_PATH)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        _atomic_write_config(config_path, render_default_config_toml(config_path), backup=False)
    else:
        ensure_default_summary_section(config_path)
        ensure_default_hotkey_section(config_path)
        ensure_default_message_sections(config_path)
    return config_path


def default_database_path_for_config(config_path: Path) -> Path:
    if config_path == expand_path(DEFAULT_CONFIG_PATH):
        return DEFAULT_DB_PATH
    return config_path.parent / "events.sqlite3"


def normalize_language(language: str) -> str:
    original = str(language).strip()
    if not original:
        raise ValueError("Language cannot be empty")
    value = original.lower()
    aliases = {
        "english": "en",
        "en-us": "en",
        "en-gb": "en",
        "russian": "ru",
        "русский": "ru",
        "ru-ru": "ru",
    }
    return aliases.get(value, original)


def language_display_name(language: str) -> str:
    normalized = normalize_language(language)
    return LANGUAGE_NAMES.get(normalized, normalized)


def copy_message_templates(templates: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {language: dict(values) for language, values in templates.items()}


def load_message_templates(value: object) -> dict[str, dict[str, str]]:
    templates = copy_message_templates(DEFAULT_MESSAGE_TEMPLATES)
    if not isinstance(value, dict):
        return templates
    for language, defaults in DEFAULT_MESSAGE_TEMPLATES.items():
        user_templates = value.get(language, {})
        if not isinstance(user_templates, dict):
            continue
        for key in defaults:
            user_value = user_templates.get(key)
            if isinstance(user_value, str) and user_value.strip():
                templates[language][key] = user_value
    return templates


def ensure_default_message_sections(config_path: Path) -> None:
    text, data = _read_config_text(config_path)

    messages = data.get("messages", {})
    missing_languages = [
        language
        for language in SUPPORTED_LANGUAGES
        if not isinstance(messages, dict) or not isinstance(messages.get(language), dict)
    ]
    if not missing_languages:
        return

    _atomic_write_config(config_path, text + default_message_sections_toml(missing_languages))


def ensure_default_summary_section(config_path: Path) -> None:
    _, data = _read_config_text(config_path)

    current = data.get("summary", {})
    missing = dict(DEFAULT_SUMMARY_CONFIG)
    if isinstance(current, dict):
        for key in current:
            missing.pop(key, None)
    if not missing:
        return
    ensure_config_section_values(config_path, "summary", missing)


def ensure_default_hotkey_section(config_path: Path) -> None:
    _, data = _read_config_text(config_path)

    current = data.get("hotkey", {})
    missing = dict(DEFAULT_HOTKEY_CONFIG)
    if isinstance(current, dict):
        for key in current:
            missing.pop(key, None)
    if not missing:
        return
    ensure_config_section_values(config_path, "hotkey", missing)


def ensure_config_section_values(
    config_path: Path,
    section: str,
    values: dict[str, str | int | float | bool],
) -> None:
    """Append any of ``values`` that are missing from ``section`` (never overwrite).

    Skips over ``'''``/``\"\"\"`` multi-line string blocks so their interior lines
    are not mistaken for section headers or keys. Writes atomically.
    """
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    output: list[str] = []
    in_section = False
    saw_section = False
    remaining = dict(values)
    multiline_delim: str | None = None

    for line in lines:
        if multiline_delim is not None:
            output.append(line)
            if multiline_delim in line:
                multiline_delim = None
            continue

        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped:
            if in_section:
                for key, value in remaining.items():
                    output.append(_toml_assignment(key, value))
                remaining.clear()
            in_section = stripped == f"[{section}]"
            saw_section = saw_section or in_section
            output.append(line)
            continue

        output.append(line)
        multiline_delim = _multiline_open_delimiter(line)

    if in_section:
        for key, value in remaining.items():
            output.append(_toml_assignment(key, value))
        remaining.clear()

    if not saw_section:
        output.extend(["", f"[{section}]"])
        for key, value in remaining.items():
            output.append(_toml_assignment(key, value))

    _atomic_write_config(config_path, "\n".join(output).rstrip() + "\n")


def default_message_sections_toml(languages: list[str]) -> str:
    lines = [""]
    for language in languages:
        lines.append(f"[messages.{language}]")
        for key, value in DEFAULT_MESSAGE_TEMPLATES[language].items():
            lines.append(_toml_assignment(key, value))
        lines.append("")
    return "\n".join(lines)


def set_config_language(path: str | os.PathLike[str] | None, language: str) -> Path:
    config_path = write_default_config(path)
    normalized = normalize_language(language)
    lines = config_path.read_text(encoding="utf-8").splitlines()

    output: list[str] = []
    in_user_section = False
    saw_user_section = False
    wrote_language = False
    multiline_delim: str | None = None

    for line in lines:
        if multiline_delim is not None:
            output.append(line)
            if multiline_delim in line:
                multiline_delim = None
            continue

        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped:
            if in_user_section and not wrote_language:
                output.append(_toml_assignment("language", normalized))
                wrote_language = True
            in_user_section = stripped == "[user]"
            saw_user_section = saw_user_section or in_user_section
            output.append(line)
            continue

        if in_user_section and stripped.startswith("language"):
            output.append(_toml_assignment("language", normalized))
            wrote_language = True
            continue

        output.append(line)
        multiline_delim = _multiline_open_delimiter(line)

    if in_user_section and not wrote_language:
        output.append(_toml_assignment("language", normalized))
        wrote_language = True

    if not saw_user_section:
        output = ["[user]", _toml_assignment("language", normalized), ""] + output

    _atomic_write_config(config_path, "\n".join(output).rstrip() + "\n")
    return config_path


def set_voice_config(
    path: str | os.PathLike[str] | None,
    *,
    backend: str | None = None,
    voice: str | None = None,
    rate: int | None = None,
    speed: float | None = None,
    model: str | None = None,
    audio_format: str | None = None,
    estimated_cost_per_minute_usd: float | None = None,
    text_input_price_per_million_tokens_usd: float | None = None,
    audio_output_price_per_million_tokens_usd: float | None = None,
    audio_tokens_per_second: float | None = None,
    instructions: str | None = None,
    api_key_env: str | None = None,
    interrupt_on_user_input: bool | None = None,
) -> Path:
    values: dict[str, str | int | float | bool] = {}
    if interrupt_on_user_input is not None:
        values["interrupt_on_user_input"] = bool(interrupt_on_user_input)
    if backend is not None:
        values["backend"] = normalize_voice_backend(backend)
    if voice is not None:
        values["voice"] = voice
    if rate is not None:
        values["rate"] = rate
    if speed is not None:
        values["speed"] = normalize_voice_speed(speed)
    if model is not None:
        values["model"] = model
    if audio_format is not None:
        values["format"] = normalize_audio_format(audio_format)
    if estimated_cost_per_minute_usd is not None:
        values["estimated_cost_per_minute_usd"] = normalize_estimated_cost(estimated_cost_per_minute_usd)
    if text_input_price_per_million_tokens_usd is not None:
        values["text_input_price_per_million_tokens_usd"] = normalize_estimated_cost(
            text_input_price_per_million_tokens_usd
        )
    if audio_output_price_per_million_tokens_usd is not None:
        values["audio_output_price_per_million_tokens_usd"] = normalize_estimated_cost(
            audio_output_price_per_million_tokens_usd
        )
    if audio_tokens_per_second is not None:
        values["audio_tokens_per_second"] = normalize_estimated_cost(audio_tokens_per_second)
    if instructions is not None:
        values["instructions"] = instructions
    if api_key_env is not None:
        values["api_key_env"] = api_key_env
    return set_config_section_values(path, "voice", values)


def set_summary_config(
    path: str | os.PathLike[str] | None,
    *,
    enabled: bool | None = None,
    provider: str | None = None,
    model: str | None = None,
    privacy_level: str | None = None,
    pipeline_log: bool | None = None,
) -> Path:
    values: dict[str, str | int | float | bool] = {}
    if enabled is not None:
        values["enabled"] = bool(enabled)
    if provider is not None:
        values["provider"] = normalize_summary_provider(provider)
    if model is not None:
        values["model"] = model
    if privacy_level is not None:
        values["privacy_level"] = normalize_summary_privacy_level(privacy_level)
    if pipeline_log is not None:
        values["pipeline_log"] = bool(pipeline_log)
    return set_config_section_values(path, "summary", values)


def set_hotkey_config(
    path: str | os.PathLike[str] | None,
    *,
    enabled: bool | None = None,
    stop_speaking: str | None = None,
) -> Path:
    """Update the [hotkey] section. ``stop_speaking`` is validated and stored canonically."""
    values: dict[str, str | int | float | bool] = {}
    if enabled is not None:
        values["enabled"] = bool(enabled)
    if stop_speaking is not None:
        values["stop_speaking"] = parse_hotkey(stop_speaking).canonical
    return set_config_section_values(path, "hotkey", values)


def set_events_config(
    path: str | os.PathLike[str] | None,
    **flags: bool,
) -> Path:
    allowed = {
        "task_finished",
        "permission_needed",
        "input_needed",
        "task_failed",
        "subagent_finished",
    }
    values: dict[str, str | int | float | bool] = {}
    for key, value in flags.items():
        if key not in allowed:
            raise ValueError(f"Unknown event flag '{key}'")
        if value is not None:
            values[key] = bool(value)
    return set_config_section_values(path, "events", values)


def set_limits_config(
    path: str | os.PathLike[str] | None,
    *,
    max_events_per_minute: int | None = None,
    daily_spend_cap_usd: float | None = None,
    monthly_spend_cap_usd: float | None = None,
) -> Path:
    """Update the [limits] section. Spend caps of 0 mean "no cap"."""
    values: dict[str, str | int | float | bool] = {}
    if max_events_per_minute is not None:
        value = int(max_events_per_minute)
        if value < 0:
            raise ValueError("max_events_per_minute must be non-negative")
        values["max_events_per_minute"] = value
    if daily_spend_cap_usd is not None:
        values["daily_spend_cap_usd"] = normalize_estimated_cost(daily_spend_cap_usd)
    if monthly_spend_cap_usd is not None:
        values["monthly_spend_cap_usd"] = normalize_estimated_cost(monthly_spend_cap_usd)
    return set_config_section_values(path, "limits", values)


def set_daemon_config(
    path: str | os.PathLike[str] | None,
    *,
    event_retention_days: int | None = None,
    max_log_bytes: int | None = None,
) -> Path:
    """Update the [daemon] section. ``event_retention_days`` of 0 keeps events forever."""
    values: dict[str, str | int | float | bool] = {}
    if event_retention_days is not None:
        value = int(event_retention_days)
        if value < 0:
            raise ValueError("event_retention_days must be non-negative")
        values["event_retention_days"] = value
    if max_log_bytes is not None:
        log_bytes = int(max_log_bytes)
        if log_bytes < 0:
            raise ValueError("max_log_bytes must be non-negative")
        values["max_log_bytes"] = log_bytes
    return set_config_section_values(path, "daemon", values)


def set_autostart_managed(path: str | os.PathLike[str] | None, managed: bool) -> Path:
    """Record whether the menu bar autostart agent is managed by us."""
    return set_config_section_values(path, "autostart", {"managed": bool(managed)})


def set_reminders_config(
    path: str | os.PathLike[str] | None,
    *,
    enabled: bool | None = None,
    margin_minutes: int | None = None,
) -> Path:
    """Update the [reminders] section (timed idle nudge)."""
    values: dict[str, str | int | float | bool] = {}
    if enabled is not None:
        values["enabled"] = bool(enabled)
    if margin_minutes is not None:
        value = int(margin_minutes)
        if value < 0:
            raise ValueError("margin_minutes must be non-negative")
        values["margin_minutes"] = value
    return set_config_section_values(path, "reminders", values)


def set_quiet_hours_config(
    path: str | os.PathLike[str] | None,
    *,
    enabled: bool | None = None,
    start: str | None = None,
    end: str | None = None,
    voice: bool | None = None,
    desktop: bool | None = None,
) -> Path:
    """Update the [quiet_hours] section. ``start``/``end`` are HH:MM in the config timezone."""
    values: dict[str, str | int | float | bool] = {}
    if enabled is not None:
        values["enabled"] = bool(enabled)
    if start is not None:
        values["from"] = normalize_hhmm(start)
    if end is not None:
        values["to"] = normalize_hhmm(end)
    if voice is not None:
        values["voice"] = bool(voice)
    if desktop is not None:
        values["desktop"] = bool(desktop)
    return set_config_section_values(path, "quiet_hours", values)


def reset_config(
    path: str | os.PathLike[str] | None,
    section: str | None = None,
) -> Path:
    """Back up the current config, then rewrite the whole file (or one ``section``)
    to its bundled defaults.

    Returns the backup :class:`Path` written before the reset. Reusing the atomic
    writer means the original is preserved if the rewritten text fails to parse.
    """
    config_path = write_default_config(path)
    backup_path = _backup_config(config_path)

    if section is None:
        _atomic_write_config(
            config_path, render_default_config_toml(config_path), backup=False
        )
        return backup_path

    default_body = _default_section_body(config_path, section)
    if default_body is None:
        raise ValueError(f"Cannot reset unknown section '{section}'")

    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    output: list[str] = []
    in_section = False
    saw_section = False
    skip_until: str | None = None
    preserve_until: str | None = None

    for line in lines:
        if skip_until is not None:
            if skip_until in line:
                skip_until = None
            continue
        if preserve_until is not None:
            output.append(line)
            if preserve_until in line:
                preserve_until = None
            continue

        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped:
            in_section = stripped == f"[{section}]"
            saw_section = saw_section or in_section
            output.append(line)
            if in_section:
                output.extend(default_body)
            continue

        if in_section:
            # Drop the prior contents of this section, including multi-line bodies,
            # so nothing of the old section leaks past the fresh defaults.
            skip_until = _multiline_open_delimiter(line)
            continue

        output.append(line)
        preserve_until = _multiline_open_delimiter(line)

    if not saw_section:
        output.append(f"[{section}]")
        output.extend(default_body)

    _atomic_write_config(config_path, "\n".join(output).rstrip() + "\n", backup=False)
    return backup_path


def set_config_section_values(
    path: str | os.PathLike[str] | None,
    section: str,
    values: dict[str, str | int | float | bool],
) -> Path:
    """Set ``values`` in ``section`` (replacing existing keys in place, appending new
    ones), writing atomically.

    Skips over ``'''``/``\"\"\"`` multi-line string blocks so their interior lines are
    never mistaken for keys or section headers and are preserved byte-for-byte.
    """
    config_path = write_default_config(path)
    lines = config_path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    in_section = False
    saw_section = False
    remaining = dict(values)
    # ``preserve_until`` keeps the interior of a multi-line block verbatim;
    # ``skip_until`` drops the interior of an old value we are replacing.
    preserve_until: str | None = None
    skip_until: str | None = None

    for line in lines:
        if skip_until is not None:
            if skip_until in line:
                skip_until = None
            continue
        if preserve_until is not None:
            output.append(line)
            if preserve_until in line:
                preserve_until = None
            continue

        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped:
            if in_section:
                for key, value in remaining.items():
                    output.append(_toml_assignment(key, value))
                remaining.clear()
            in_section = stripped == f"[{section}]"
            saw_section = saw_section or in_section
            output.append(line)
            continue

        if in_section:
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if key in remaining:
                output.append(_toml_assignment(key, remaining.pop(key)))
                # If the old value opened a multi-line block, drop its body so it
                # is not duplicated after the replacement assignment.
                skip_until = _multiline_open_delimiter(line)
                continue

        output.append(line)
        preserve_until = _multiline_open_delimiter(line)

    if in_section:
        for key, value in remaining.items():
            output.append(_toml_assignment(key, value))
        remaining.clear()

    if not saw_section:
        output.extend(["", f"[{section}]"])
        for key, value in remaining.items():
            output.append(_toml_assignment(key, value))

    _atomic_write_config(config_path, "\n".join(output).rstrip() + "\n")
    return config_path


def normalize_voice_backend(backend: str) -> str:
    normalized = backend.strip().lower()
    supported = {"macos_say", "openai_tts"}
    if normalized not in supported:
        raise ValueError(f"Unsupported voice backend '{backend}'. Supported: {', '.join(sorted(supported))}")
    return normalized


def normalize_summary_provider(provider: str) -> str:
    normalized = str(provider).strip().lower()
    if normalized not in SUMMARY_PROVIDERS:
        supported = ", ".join(SUMMARY_PROVIDERS)
        raise ValueError(f"Unsupported summary provider '{provider}'. Supported: {supported}")
    return normalized


def normalize_summary_privacy_level(privacy_level: str) -> str:
    normalized = str(privacy_level).strip().lower()
    if normalized not in SUMMARY_PRIVACY_LEVELS:
        supported = ", ".join(SUMMARY_PRIVACY_LEVELS)
        raise ValueError(f"Unsupported summary privacy level '{privacy_level}'. Supported: {supported}")
    return normalized


def normalize_audio_format(audio_format: str) -> str:
    normalized = audio_format.strip().lower()
    supported = {"mp3", "opus", "aac", "flac", "wav", "pcm"}
    if normalized not in supported:
        raise ValueError(f"Unsupported audio format '{audio_format}'. Supported: {', '.join(sorted(supported))}")
    return normalized


def normalize_voice_speed(speed: float) -> float:
    if speed < 0.25 or speed > 4.0:
        raise ValueError("Voice speed must be between 0.25 and 4.0")
    return round(speed, 2)


def normalize_estimated_cost(cost: float) -> float:
    if cost < 0:
        raise ValueError("Estimated cost must be non-negative")
    return round(cost, 6)


_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def normalize_hhmm(value: object) -> str:
    """Validate a 24-hour ``HH:MM`` clock time and return it zero-padded."""
    text = str(value).strip()
    match = _HHMM_RE.match(text)
    if match is None:
        raise ValueError(f"Time must be in 24-hour HH:MM format, got '{value}'")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"Time out of range (00:00–23:59), got '{value}'")
    return f"{hour:02d}:{minute:02d}"


def _multiline_open_delimiter(line: str) -> str | None:
    """Return ``'''`` or ``\"\"\"`` if ``line`` opens an unterminated multi-line TOML
    string, else ``None``.

    A line such as ``prompt = '''`` (or one whose triple-quote is not closed on the
    same line) starts a block whose interior lines must not be parsed as keys.

    The triple-quote is only an opener when the assigned VALUE itself begins with it.
    A single-line value that merely *contains* a triple-quote inside an
    already-quoted string (e.g. ``instructions = "use ''' for blocks"``) is not an
    opener — treating it as one would put every later line into a phantom
    multi-line state. We therefore inspect the value portion after the first
    ``'='`` rather than scanning the whole line.
    """
    eq = line.find("=")
    if eq == -1:
        return None
    value = line[eq + 1 :].lstrip()
    for delim in ("'''", '"""'):
        if not value.startswith(delim):
            continue
        # Closed on the same line (e.g. ``x = '''one-liner'''``)?
        if value.find(delim, len(delim)) != -1:
            return None
        return delim
    return None


def _toml_assignment(key: str, value: str | int | float | bool) -> str:
    if isinstance(value, bool):
        return f"{key} = {str(value).lower()}"
    if isinstance(value, int):
        return f"{key} = {value}"
    if isinstance(value, float):
        return f"{key} = {value:g}"
    if "\n" in value:
        return f"{key} = '''\n{value.rstrip()}\n'''"
    return f'{key} = "{_escape_toml_string(value)}"'


def _escape_toml_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _default_section_body(config_path: Path, section: str) -> list[str] | None:
    """Return the body lines (excluding the ``[section]`` header) of ``section`` from
    the rendered default template, or ``None`` if the template has no such section.

    Used by :func:`reset_config` so any template section — including those with
    multi-line values like ``[summary]`` — restores to its exact bundled default.
    """
    lines = render_default_config_toml(config_path).splitlines()
    body: list[str] = []
    in_section = False
    found = False
    multiline_delim: str | None = None
    for line in lines:
        if multiline_delim is not None:
            if in_section:
                body.append(line)
            if multiline_delim in line:
                multiline_delim = None
            continue
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped:
            if in_section:
                break
            in_section = stripped == f"[{section}]"
            found = found or in_section
            continue
        if in_section:
            body.append(line)
            multiline_delim = _multiline_open_delimiter(line)
    if not found:
        return None
    # Trim trailing blank lines that separated this section from the next.
    while body and not body[-1].strip():
        body.pop()
    return body


def _section_defaults(section: str) -> dict[str, str | int | float | bool] | None:
    """Return a fresh copy of the bundled defaults for ``section``, or ``None`` if the
    section has no flat default template (e.g. ``[user]``/``[messages.*]``)."""
    registry: dict[str, dict[str, str | int | float | bool]] = {
        "summary": DEFAULT_SUMMARY_CONFIG,
        "hotkey": DEFAULT_HOTKEY_CONFIG,
        "daemon": DEFAULT_DAEMON_CONFIG,
        "limits": DEFAULT_LIMITS_CONFIG,
        "events": DEFAULT_EVENTS_CONFIG,
        "quiet_hours": DEFAULT_QUIET_HOURS_CONFIG,
        "autostart": DEFAULT_AUTOSTART_CONFIG,
        "reminders": DEFAULT_REMINDERS_CONFIG,
    }
    defaults = registry.get(section)
    return dict(defaults) if defaults is not None else None


# Sections whose missing keys are back-filled on load, in a deterministic order so
# the file grows predictably across versions.
_MIGRATION_SECTIONS: tuple[str, ...] = (
    "daemon",
    "summary",
    "hotkey",
    "limits",
    "events",
    "quiet_hours",
    "autostart",
    "reminders",
)

# Old default idle_reminder text (per language) that predates the short one-liner.
# Existing configs that still carry the verbose default are upgraded in place so
# they stop speaking the long "cache is still warm" sentence; a user's CUSTOM text
# (anything not exactly equal to these) is never touched.
_STALE_IDLE_REMINDER_DEFAULTS: dict[str, str] = {
    "en": "Just a reminder: {project} is done and waiting for your reply — within about {minutes} minutes, while {agent}'s cache is still warm.",
    "ru": "Напоминаю: {project} завершён и ждёт твоего ответа — примерно в течение {minutes} минут, пока кэш {agent} ещё тёплый.",
}


def _stale_default_upgrades(data: dict) -> dict[str, dict[str, str]]:
    """Section keys whose value equals a known OLD default and should be replaced
    with the current bundled default. Custom values are never matched, so a user's
    edits are preserved."""
    upgrades: dict[str, dict[str, str]] = {}

    messages = data.get("messages", {})
    if isinstance(messages, dict):
        for lang, old_default in _STALE_IDLE_REMINDER_DEFAULTS.items():
            lang_messages = messages.get(lang, {})
            if not isinstance(lang_messages, dict):
                continue
            if lang_messages.get("idle_reminder") == old_default:
                new_default = DEFAULT_MESSAGE_TEMPLATES.get(lang, {}).get("idle_reminder")
                if new_default and new_default != old_default:
                    upgrades.setdefault(f"messages.{lang}", {})["idle_reminder"] = new_default

    summary = data.get("summary", {})
    if isinstance(summary, dict):
        current_prompt = summary.get("prompt")
        if isinstance(current_prompt, str):
            normalized = current_prompt.strip()
            is_stale = any(normalized == stale.strip() for stale in _STALE_SUMMARY_PROMPTS)
            if is_stale and normalized != DEFAULT_SUMMARY_PROMPT.strip():
                upgrades.setdefault("summary", {})["prompt"] = DEFAULT_SUMMARY_PROMPT

    return upgrades


def _migrate_config(config_path: Path, data: dict) -> dict:
    """Back-fill any missing default keys for known sections and stamp the schema
    version, writing the file atomically only when something actually changes.

    Idempotent: a fully populated, current-version file is left untouched.
    """
    additions: dict[str, dict[str, str | int | float | bool]] = {}
    for section in _MIGRATION_SECTIONS:
        defaults = _section_defaults(section)
        if defaults is None:
            continue
        current = data.get(section, {})
        if not isinstance(current, dict):
            current = {}
        missing = {key: value for key, value in defaults.items() if key not in current}
        if missing:
            additions[section] = missing

    upgrades = _stale_default_upgrades(data)

    meta = data.get("meta", {})
    stored_version = meta.get("schema_version") if isinstance(meta, dict) else None
    needs_version = stored_version != CONFIG_SCHEMA_VERSION

    if not additions and not upgrades and not needs_version:
        return data

    try:
        for section, missing in additions.items():
            ensure_config_section_values(config_path, section, missing)
        for section, replacements in upgrades.items():
            # Replace (not just back-fill) the stale default value in place.
            set_config_section_values(config_path, section, replacements)
        if needs_version:
            ensure_config_section_values(config_path, "meta", {"schema_version": CONFIG_SCHEMA_VERSION})
    except OSError:
        # A read-only config location must not break loading; the in-memory
        # defaults already cover any keys we failed to persist.
        for section, missing in additions.items():
            merged = dict(data.get(section, {})) if isinstance(data.get(section), dict) else {}
            merged.update(missing)
            data[section] = merged
        return data

    _, refreshed = _read_config_text(config_path)
    return refreshed


def _backup_config(config_path: Path) -> Path:
    """Copy ``config_path`` to ``<name>.bak-YYYYMMDDHHMMSS`` and return the backup.

    The filename is uniquified within a second (``-2``, ``-3``, ...) so a rapid
    sequence of writes never overwrites an earlier backup.
    """
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = config_path.with_name(f"{config_path.name}.bak-{timestamp}")
    counter = 2
    while backup_path.exists():
        backup_path = config_path.with_name(f"{config_path.name}.bak-{timestamp}-{counter}")
        counter += 1
    backup_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    backup_path.chmod(0o600)
    return backup_path


def _backup_sort_key(config_name: str, backup: Path) -> tuple[str, int]:
    """Sort key for a ``<name>.bak-<timestamp>[-<counter>]`` file.

    Returns ``(timestamp, counter)`` so same-second backups order by their numeric
    counter (``-10`` after ``-2``) rather than lexically by name.
    """
    suffix = backup.name[len(f"{config_name}.bak-"):]
    timestamp, _, counter = suffix.partition("-")
    try:
        counter_value = int(counter) if counter else 1
    except ValueError:
        counter_value = 1
    return (timestamp, counter_value)


def list_config_backups(path: str | os.PathLike[str] | None = None) -> list[Path]:
    """Return existing ``config.toml.bak-*`` backups, newest first."""
    config_path = expand_path(path or DEFAULT_CONFIG_PATH)
    pattern = f"{config_path.name}.bak-*"
    backups = list(config_path.parent.glob(pattern))
    return sorted(
        backups,
        key=lambda p: _backup_sort_key(config_path.name, p),
        reverse=True,
    )


def restore_config_backup(
    path: str | os.PathLike[str] | None = None,
    backup: str | os.PathLike[str] | None = None,
) -> Path:
    """Restore the config from a backup, backing up the current file first.

    With ``backup`` falsy, the most recent ``config.toml.bak-*`` is used. The
    restored text is TOML-syntax validated before it replaces the live file (an
    invalid backup raises :class:`ConfigError` and leaves the config untouched);
    out-of-range values still surface as :class:`ConfigError` at the next load.
    Returns the backup that was restored.
    """
    config_path = expand_path(path or DEFAULT_CONFIG_PATH)
    if backup:
        backup_path = Path(backup).expanduser()
        if not backup_path.is_absolute() and not backup_path.exists():
            # Allow passing just the backup filename (resolved in the config dir),
            # but only when the given path itself does not exist.
            backup_path = config_path.with_name(backup_path.name)
    else:
        backups = list_config_backups(config_path)
        if not backups:
            raise ConfigError(
                "No config backups found to restore.",
                path=config_path,
                hint="Backups appear as config.toml.bak-* after any config change.",
            )
        backup_path = backups[0]

    if not backup_path.exists():
        raise ConfigError(
            f"Backup not found: {backup_path}",
            path=config_path,
            hint="Run `voiccce config --list-backups` to see available backups.",
        )

    text = backup_path.read_text(encoding="utf-8")
    # Backs up the current file (when present) before replacing it.
    _atomic_write_config(config_path, text, backup=True)
    return backup_path


def _atomic_write_config(config_path: Path, text: str, *, backup: bool = True) -> None:
    """Atomically write ``text`` to ``config_path``.

    The text is written to a temp file in the SAME directory, fsync'd, and validated
    with :func:`tomllib.loads` before being moved into place with :func:`os.replace`.
    On a parse failure a :class:`ConfigError` is raised and the original file is left
    untouched. When ``backup`` is true and the target already exists, it is first
    copied to ``<name>.bak-YYYYMMDDHHMMSS``. The final file is chmod 0o600.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Refusing to write invalid TOML: {getattr(exc, 'msg', None) or exc}",
            path=config_path,
            line=getattr(exc, "lineno", None),
            hint="This is a bug in the config writer; the original file was left unchanged.",
        ) from exc

    if backup and config_path.exists():
        _backup_config(config_path)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(config_path.parent),
        prefix=f".{config_path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
