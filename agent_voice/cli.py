from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote, urlparse

from .config import (
    AgentVoiceConfig,
    language_display_name,
    load_config,
    normalize_language,
    set_config_language,
    set_hotkey_config,
    set_voice_config,
    write_default_config,
)
from .hotkey import DEFAULT_STOP_SPEAKING_HOTKEY, HOTKEY_PRESETS, format_hotkey_display
from .daemon import process_once, run_daemon
from .db import connect, init_db
from .delivery import DeliveryRouter, test_message
from .hooks.claude_event_collector import read_event_from_stdin as read_claude_event_from_stdin
from .hooks.codex_event_collector import read_event_from_stdin as read_codex_event_from_stdin
from .hooks.pi_event_collector import read_event_from_stdin as read_pi_event_from_stdin
from .installer import WrapperImportError
from .installer.claude_code import install_claude_code_personal
from .installer.codex import install_codex_personal
from .installer.pi import install_pi_personal
from .models import EventType, NormalizedEvent
from .queue import enqueue_event
from .runtime import (
    clear_voice_mute,
    parse_duration_seconds,
    set_voice_mute,
    stop_speaking,
    voice_mute_status,
    voice_session_active,
)
from .secrets import (
    OpenAIKeyValidation,
    delete_openai_keychain_secret,
    get_openai_secret_status,
    resolve_openai_api_key,
    set_openai_keychain_secret,
    validate_openai_tts_key,
)
from .ui import Choice, checkbox_select, select_one
from .service import (
    daemon_status,
    menubar_service_paths,
    menubar_status,
    service_paths,
    start_daemon,
    start_menubar,
    stop_daemon,
    stop_menubar,
)
from .usage import fetch_usage_stats, format_duration, format_usd


# Integrations offered by `voiccce setup`. Order is preserved in the picker.
SETUP_TARGETS: list[Choice] = [
    Choice("claude-code", "Claude Code", "Anthropic's terminal coding agent"),
    Choice("codex", "Codex", "OpenAI's coding agent"),
    Choice("pi", "pi", "Earendil Works coding agent"),
]
_SETUP_TARGET_LABEL = {choice.value: choice.label for choice in SETUP_TARGETS}
_SETUP_TARGET_ORDER = [choice.value for choice in SETUP_TARGETS]

# Voice backends offered by the `voiccce setup` voice picker.
VOICE_BACKENDS: list[Choice] = [
    Choice(
        "openai_tts",
        "OpenAI TTS",
        "Natural cloud voice (recommended). Needs an OpenAI API key.",
    ),
    Choice(
        "macos_say",
        "macOS built-in voice",
        "Offline and free. Uses the system 'say' voice, no API key.",
    ),
]
# Default voice name per backend when the user does not pass --voice.
_DEFAULT_VOICE = {"openai_tts": "marin", "macos_say": "Alex"}
# Yes/No options for radio prompts where `esc` must cancel (unlike confirm(),
# which maps cancel to its default).
_YES_NO: list[Choice] = [Choice("yes", "Yes"), Choice("no", "No")]

# Words that mean "turn the stop-speaking hotkey off" when passed to --hotkey.
_HOTKEY_OFF_TOKENS = {"off", "none", "no", "disable", "disabled", "false", "0", ""}
# Short hints shown beside each preset in the setup picker.
_HOTKEY_PRESET_HINTS = {
    "alt+cmd+s": "easy two-key combo",
    "ctrl+alt+cmd+s": "three modifiers, no conflicts",
    "ctrl+alt+cmd+.": "“.” reads as stop",
    "alt+cmd+.": "easy two-key combo",
}


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return
    args.handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voiccce")
    parser.add_argument("--config", help="Path to config.toml")
    subparsers = parser.add_subparsers(dest="command")

    install = subparsers.add_parser("install", help="Create local config and database")
    install.add_argument("target", nargs="?", choices=["claude-code", "codex", "pi"], help="Optional integration to install")
    install.add_argument("--scope", default="personal", choices=["personal"], help="Claude settings scope")
    install.add_argument(
        "--claude-config-dir",
        help="Claude config directory, e.g. ~/.claude-personal. Uses <dir>/settings.json.",
    )
    install.add_argument("--settings-path", help="Direct path to Claude settings.json")
    install.add_argument("--codex-home", help="Codex home directory, e.g. ~/.codex-personal. Uses <dir>/hooks.json.")
    install.add_argument("--hooks-path", help="Direct path to Codex hooks.json")
    install.add_argument("--pi-home", help="pi home directory (default ~/.pi; honors PI_CODING_AGENT_DIR for profiles like pi-personal). Installs a global extension.")
    install.set_defaults(handler=cmd_install)

    setup = subparsers.add_parser(
        "setup",
        help="One command to set up everything: OpenAI key, voice, hooks, daemon, and a test",
    )
    setup.add_argument(
        "target",
        nargs="?",
        choices=["claude-code", "codex", "pi", "both"],
        help="Which agent(s) to wire hooks for. Omit for an interactive "
        "checkbox picker. 'both' is a legacy alias for claude-code + codex.",
    )
    setup.add_argument("--language", help="Notification language, e.g. English, Russian, Spanish, or Japanese")
    setup.add_argument("--voice", default=None, help="Voice name (default: marin for OpenAI TTS, Alex for --local)")
    voice_backend_group = setup.add_mutually_exclusive_group()
    voice_backend_group.add_argument(
        "--local",
        action="store_true",
        help="Use the local macOS say voice instead of OpenAI TTS (no API key); skips the voice picker",
    )
    voice_backend_group.add_argument(
        "--openai",
        action="store_true",
        help="Use OpenAI TTS (the premium cloud voice); skips the voice picker",
    )
    setup.add_argument(
        "--hotkey",
        help="Global stop-speaking hotkey, e.g. 'alt+cmd+s' or 'ctrl+alt+cmd+.'; "
        "use 'off' to disable. Omit for an interactive picker.",
    )
    setup.add_argument("--reset-key", action="store_true", help="Prompt for a new OpenAI key even if one is already configured")
    setup.add_argument("--no-test", action="store_true", help="Skip the test notification at the end")
    setup.add_argument(
        "--menubar",
        dest="menubar",
        action="store_true",
        default=None,
        help="Install and start the macOS menu bar app (prompted if omitted)",
    )
    setup.add_argument(
        "--no-menubar",
        dest="menubar",
        action="store_false",
        help="Skip the macOS menu bar app",
    )
    setup.add_argument(
        "--claude-config-dir",
        help="Claude config directory, e.g. ~/.claude-personal. Uses <dir>/settings.json.",
    )
    setup.add_argument("--settings-path", help="Direct path to Claude settings.json")
    setup.add_argument("--codex-home", help="Codex home directory, e.g. ~/.codex-personal. Uses <dir>/hooks.json.")
    setup.add_argument("--hooks-path", help="Direct path to Codex hooks.json")
    setup.add_argument("--pi-home", help="pi home directory (default ~/.pi; honors PI_CODING_AGENT_DIR for profiles like pi-personal). Installs a global extension.")
    setup.set_defaults(handler=cmd_setup)

    start = subparsers.add_parser("start", help="Start daemon in the background")
    start.set_defaults(handler=cmd_start)

    stop = subparsers.add_parser("stop", help="Stop background daemon")
    stop.set_defaults(handler=cmd_stop)

    update = subparsers.add_parser("update", help="Update this installation from a local checkout")
    update.add_argument(
        "--source",
        help="Path to the voiccce checkout. Defaults to the current directory or the original install source.",
    )
    update.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart daemon/menu bar after updating.",
    )
    update.set_defaults(handler=cmd_update)

    menubar = subparsers.add_parser("menubar", help="Run menu bar companion in the foreground")
    menubar.set_defaults(handler=cmd_menubar)

    menubar_start = subparsers.add_parser("menubar-start", help="Start menu bar companion in the background")
    menubar_start.set_defaults(handler=cmd_menubar_start)

    menubar_stop = subparsers.add_parser("menubar-stop", help="Stop menu bar companion")
    menubar_stop.set_defaults(handler=cmd_menubar_stop)

    menubar_status_cmd = subparsers.add_parser("menubar-status", help="Show menu bar companion status")
    menubar_status_cmd.set_defaults(handler=cmd_menubar_status)

    stop_speech = subparsers.add_parser("stop-speaking", help="Stop current voice playback")
    stop_speech.set_defaults(handler=cmd_stop_speaking)

    mute = subparsers.add_parser("mute", help="Temporarily mute voice playback")
    mute.add_argument("--for", dest="duration", default="10m", help="Duration like 30s, 10m, or 1h")
    mute.set_defaults(handler=cmd_mute)

    unmute = subparsers.add_parser("unmute", help="Enable voice playback")
    unmute.set_defaults(handler=cmd_unmute)

    status = subparsers.add_parser("status", help="Show queue and adapter status")
    status.set_defaults(handler=cmd_status)

    config_cmd = subparsers.add_parser("config", help="Show or update local configuration")
    config_cmd.add_argument("--language", help="Notification language, e.g. English, Russian, Spanish, or Japanese")
    config_cmd.add_argument("--voice-backend", choices=["macos_say", "openai_tts"], help="Voice backend")
    config_cmd.add_argument("--voice", help="Voice name, e.g. Alex, marin, cedar")
    config_cmd.add_argument("--voice-rate", type=int, help="macOS say voice rate")
    config_cmd.add_argument("--voice-speed", type=float, help="Cloud TTS speed, from 0.25 to 4.0")
    config_cmd.add_argument("--voice-model", help="Cloud TTS model")
    config_cmd.add_argument("--voice-format", choices=["mp3", "opus", "aac", "flac", "wav", "pcm"], help="Audio output format")
    config_cmd.add_argument("--voice-estimated-cost-per-minute", type=float, help="Legacy estimated OpenAI TTS cost per generated audio minute")
    config_cmd.add_argument("--voice-text-input-price-per-million", type=float, help="OpenAI TTS text input price per 1M tokens")
    config_cmd.add_argument("--voice-audio-output-price-per-million", type=float, help="OpenAI TTS audio output price per 1M audio tokens")
    config_cmd.add_argument("--voice-audio-tokens-per-second", type=float, help="Estimated generated audio tokens per second")
    config_cmd.add_argument("--voice-instructions", help="Cloud TTS speaking style instructions")
    config_cmd.add_argument("--voice-api-key-env", help="Environment variable that contains the API key")
    config_cmd.add_argument(
        "--hotkey",
        help="Global stop-speaking hotkey, e.g. 'alt+cmd+s' or 'ctrl+alt+cmd+.'; use 'off' to disable",
    )
    config_cmd.set_defaults(handler=cmd_config)

    secret = subparsers.add_parser("secret", help="Manage local secrets in macOS Keychain")
    secret_subparsers = secret.add_subparsers(dest="secret_command")
    secret_set = secret_subparsers.add_parser("set", help="Store a secret in macOS Keychain")
    secret_set.add_argument("name", choices=["openai"])
    secret_set.set_defaults(handler=cmd_secret_set)
    secret_status = secret_subparsers.add_parser("status", help="Show whether a secret is configured")
    secret_status.add_argument("name", choices=["openai"])
    secret_status.set_defaults(handler=cmd_secret_status)
    secret_delete = secret_subparsers.add_parser("delete", help="Delete a secret from macOS Keychain")
    secret_delete.add_argument("name", choices=["openai"])
    secret_delete.set_defaults(handler=cmd_secret_delete)

    daemon = subparsers.add_parser("daemon", help="Run daemon")
    daemon.add_argument("--once", action="store_true", help="Process one batch and exit")
    daemon.add_argument("--no-deliver", action="store_true", help="Create notification records without delivery")
    daemon.add_argument("--terminal-only", action="store_true", help="Deliver only to terminal log")
    daemon.set_defaults(handler=cmd_daemon)

    test = subparsers.add_parser("test", help="Send a test notification")
    test.add_argument("--terminal-only", action="store_true", help="Print instead of voice/desktop")
    test.set_defaults(handler=cmd_test)

    events = subparsers.add_parser("events", help="List recent events")
    events.add_argument("--limit", type=int, default=20)
    events.set_defaults(handler=cmd_events)

    collect = subparsers.add_parser("collect", help="Read a hook payload from stdin and enqueue it")
    collect.add_argument("agent", choices=["claude-code", "codex", "pi"])
    collect.add_argument(
        "--hook",
        default="Stop",
        choices=[
            "Stop",
            "Notification",
            "PermissionRequest",
            "PermissionDenied",
            "StopFailure",
            "SubagentStop",
            "UserPromptSubmit",
            "SessionStart",
        ],
    )
    collect.set_defaults(handler=cmd_collect)

    enqueue_test = subparsers.add_parser("enqueue-test-event", help="Enqueue a synthetic event")
    enqueue_test.add_argument("--type", default=EventType.TASK_FINISHED.value)
    enqueue_test.add_argument("--project", default="voiccce")
    enqueue_test.add_argument("--session", default="test-session")
    enqueue_test.add_argument("--ask", default=None)
    enqueue_test.set_defaults(handler=cmd_enqueue_test_event)

    return parser


def _claude_install_kwargs(args: argparse.Namespace) -> dict[str, Path]:
    if args.settings_path:
        return {"settings_path": Path(args.settings_path).expanduser()}
    if args.claude_config_dir:
        return {"settings_path": Path(args.claude_config_dir).expanduser() / "settings.json"}
    return {}


def _codex_install_kwargs(args: argparse.Namespace) -> dict[str, Path]:
    kwargs: dict[str, Path] = {}
    if args.hooks_path:
        kwargs["hooks_path"] = Path(args.hooks_path).expanduser()
    if args.codex_home:
        kwargs["codex_home"] = Path(args.codex_home).expanduser()
    return kwargs


def _pi_install_kwargs(args: argparse.Namespace) -> dict[str, Path]:
    kwargs: dict[str, Path] = {}
    if getattr(args, "pi_home", None):
        kwargs["pi_home"] = Path(args.pi_home).expanduser()
    return kwargs


def cmd_install(args: argparse.Namespace) -> None:
    try:
        _cmd_install(args)
    except WrapperImportError as exc:
        raise SystemExit(str(exc))


def _cmd_install(args: argparse.Namespace) -> None:
    if args.target == "claude-code":
        result = install_claude_code_personal(verify=True, **_claude_install_kwargs(args))
        print(f"Claude Code personal settings: {result.settings_path}")
        print(f"Backup: {result.backup_path}")
        print(f"Hook wrapper: {result.wrapper_path}")
        print(f"Config: {result.config_path}")
        print(f"Database: {result.database_path}")
        print("Installed hooks: " + ", ".join(result.installed_events))
        return

    if args.target == "codex":
        result = install_codex_personal(verify=True, **_codex_install_kwargs(args))
        print(f"Codex hooks: {result.hooks_path}")
        print(f"Backup: {result.backup_path}")
        print(f"Hook wrapper: {result.wrapper_path}")
        print(f"Config: {result.config_path}")
        print(f"Database: {result.database_path}")
        print("Installed hooks: " + ", ".join(result.installed_events))
        print("Restart Codex app or app-server if it was already running.")
        print("Review and trust the new hook in Codex with /hooks before normal runs.")
        return

    if args.target == "pi":
        result = install_pi_personal(verify=True, **_pi_install_kwargs(args))
        print(f"pi extension: {result.extension_path}")
        print(f"Hook wrapper: {result.wrapper_path}")
        print(f"Config: {result.config_path}")
        print(f"Database: {result.database_path}")
        print("Wired events: " + ", ".join(result.installed_events))
        print("Restart pi (or run /reload) so it picks up the new extension.")
        return

    config_path = write_default_config(args.config)
    config = load_config(config_path)
    conn = connect(config.database_path)
    try:
        init_db(conn)
    finally:
        conn.close()
    print(f"Config: {config_path}")
    print(f"Database: {config.database_path}")


def cmd_setup(args: argparse.Namespace) -> None:
    config_path = write_default_config(args.config)
    config = load_config(config_path)

    # ── gather every interactive decision up front, then execute ──────────────
    targets = _resolve_setup_targets(args.target)
    if not targets:
        return
    language_choice = _resolve_setup_language(args, default=config.language)
    backend = _resolve_voice_backend(args)
    menubar_choice = _resolve_menubar_choice(args)
    hotkey_choice = _resolve_stop_hotkey(args, menubar_enabled=bool(menubar_choice))

    labels = ", ".join(
        _SETUP_TARGET_LABEL[t] for t in _SETUP_TARGET_ORDER if t in targets
    )
    print(f"→ Wiring hooks for: {labels}")

    if backend == "openai_tts":
        _ensure_openai_key(
            config,
            reset=args.reset_key,
            voice=args.voice or _DEFAULT_VOICE["openai_tts"],
        )

    if language_choice is not None:
        config_path = _apply_setup_language(config_path, language_choice)

    if backend == "macos_say":
        voice = args.voice or _DEFAULT_VOICE["macos_say"]
        set_voice_config(config_path, backend="macos_say", voice=voice)
        print(f"✓ Voice backend: macos_say (voice: {voice}, local macOS voice, no API key)")
    else:
        voice = args.voice or _DEFAULT_VOICE["openai_tts"]
        set_voice_config(config_path, backend="openai_tts", voice=voice)
        print(f"✓ Voice backend: openai_tts (voice: {voice})")

    _apply_stop_hotkey(config_path, hotkey_choice)

    installed: list[str] = []
    if "claude-code" in targets:
        result = _setup_install(
            "Claude settings.json",
            install_claude_code_personal,
            config_path=config_path,
            verify=True,
            **_claude_install_kwargs(args),
        )
        print(f"✓ Claude Code hooks → {result.settings_path}")
        installed.append("claude-code")
    if "codex" in targets:
        result = _setup_install(
            "Codex hooks.json",
            install_codex_personal,
            config_path=config_path,
            verify=True,
            **_codex_install_kwargs(args),
        )
        print(f"✓ Codex hooks → {result.hooks_path}")
        installed.append("codex")
    if "pi" in targets:
        result = _setup_install(
            "pi extension",
            install_pi_personal,
            config_path=config_path,
            verify=True,
            **_pi_install_kwargs(args),
        )
        print(f"✓ pi extension → {result.extension_path}")
        installed.append("pi")

    config = load_config(config_path)
    pid = start_daemon(config)
    print(f"✓ Daemon started (pid {pid})")

    # Finish any install work (incl. the menu bar dependency) before the test, so
    # the audible test notification is the last thing the wizard does.
    _maybe_setup_menubar(config, choice=menubar_choice)

    if not args.no_test:
        results = DeliveryRouter(config).deliver("Voiccce is ready.")
        if any(result.spoken for result in results):
            print("✓ Test sent — you should hear it now.")
        else:
            error = next((result.error for result in results if result.error), None)
            detail = f" ({error})" if error else ""
            print(f"! Test could not play audio{detail}. Check `voiccce status` and your OpenAI key.")

    print(f"\nDone. Edit {config.config_path} to customize voice, messages, and summaries.")
    if "claude-code" in installed:
        print("Claude Code: if a session was already open, start a new one so it loads the hooks.")
    if "codex" in installed:
        print("Codex: open /hooks and trust the Voiccce hooks; restart codex app-server if it was running.")
    if "pi" in installed:
        print(f"pi: restart pi (or run /reload) so it loads the extension at {result.extension_path.parent}.")


_InstallResult = TypeVar("_InstallResult")


def _setup_install(label: str, install: Callable[..., _InstallResult], **kwargs: object) -> _InstallResult:
    try:
        return install(**kwargs)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Could not parse your existing {label} (invalid JSON): {exc}. "
            "Fix or remove the file, then re-run `voiccce setup`."
        )
    except WrapperImportError as exc:
        raise SystemExit(str(exc))
    except OSError as exc:
        raise SystemExit(f"Could not update your {label}: {exc}.")


def _ensure_openai_key(config: AgentVoiceConfig, *, reset: bool, voice: str | None = None) -> None:
    key, status = resolve_openai_api_key(config)
    if key and status.available and not reset:
        validation = _validate_openai_key(config, key, voice=voice)
        if validation.ok:
            print(f"✓ Using existing OpenAI key (from {status.source})")
            return
        message = f"Existing OpenAI key from {status.source} failed validation: {validation.error}"
        if not _interactive():
            raise SystemExit(f"{message}. Re-run with `--reset-key` or update the key.")
        print(f"! {message}")

    key = getpass.getpass("OpenAI API key: ").strip()
    if not key:
        raise SystemExit("No key entered. Re-run `voiccce setup`, or use `--local` for the macOS voice.")
    validation = _validate_openai_key(config, key, voice=voice)
    if not validation.ok:
        raise SystemExit(f"OpenAI key validation failed: {validation.error}")
    try:
        set_openai_keychain_secret(config, key)
    except RuntimeError as exc:
        raise SystemExit(
            f"Could not save the key to macOS Keychain: {exc}. "
            "Put it in ~/.voiccce/.env as OPENAI_API_KEY=... instead."
        )
    print("✓ OpenAI key saved to macOS Keychain")


def _validate_openai_key(
    config: AgentVoiceConfig,
    key: str,
    *,
    voice: str | None = None,
) -> OpenAIKeyValidation:
    print("  Checking OpenAI key with a short TTS generation...")
    validation = validate_openai_tts_key(
        config,
        key,
        voice=voice or _openai_validation_voice(config),
    )
    if validation.ok:
        print("✓ OpenAI key can generate TTS audio")
    return validation


def _openai_validation_voice(config: AgentVoiceConfig) -> str:
    if config.voice_backend == "openai_tts" and config.voice_name:
        return config.voice_name
    return _DEFAULT_VOICE["openai_tts"]


def _interactive() -> bool:
    """True when both stdin and stdout are real TTYs, so prompts can be shown."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _resolve_setup_targets(target: str | None) -> set[str]:
    """Resolve which integrations to wire. Runs the interactive picker when omitted."""
    if target:
        if target == "both":
            return {"claude-code", "codex"}
        return {target}

    if not _interactive():
        return {"claude-code", "codex"}

    selected = checkbox_select(
        SETUP_TARGETS,
        title="Voiccce setup",
        subtitle="Choose what to wire hooks for",
        default=["claude-code", "codex"],
        min_selected=1,
        confirm_label="install",
    )
    if not selected:
        raise SystemExit(0)
    return set(selected)


def _resolve_voice_backend(args: argparse.Namespace) -> str:
    """Resolve the voice backend. Runs the interactive picker when no flag is given."""
    if args.local:
        return "macos_say"
    if getattr(args, "openai", False):
        return "openai_tts"
    if not _interactive():
        return "openai_tts"  # historical default for non-interactive setup

    choice = select_one(
        VOICE_BACKENDS,
        title="Voiccce setup",
        subtitle="Choose the voice",
        default="openai_tts",
    )
    if choice is None:
        raise SystemExit(0)
    return choice


def _resolve_setup_language(args: argparse.Namespace, *, default: str) -> str | None:
    """Resolve the target notification language for setup.

    Non-interactive setup preserves the existing config unless ``--language`` is
    passed. Interactive setup lets the user type any language name.
    """
    if getattr(args, "language", None):
        return args.language
    if not _interactive():
        return None

    default_display = language_display_name(default)
    try:
        entered = input(f"Notification language [{default_display}]: ").strip()
    except EOFError:
        return None
    return entered or default


def _apply_setup_language(config_path: Path, language: str) -> Path:
    try:
        updated = set_config_language(config_path, language)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print(f"✓ Notification language: {language_display_name(load_config(updated).language)}")
    return updated


def _resolve_menubar_choice(args: argparse.Namespace) -> bool | None:
    """Resolve whether to install the menu bar app. Prompts up front when possible.

    Returns ``True``/``False`` for an explicit decision, or ``None`` to defer to
    ``_maybe_setup_menubar`` (non-macOS or non-interactive, which both skip it).
    """
    if args.menubar is not None:
        return args.menubar
    if sys.platform != "darwin" or not _interactive():
        return None
    # select_one (not confirm) so `esc` aborts the wizard like the other menus,
    # rather than silently falling through to the "yes, install" default.
    choice = select_one(_YES_NO, title="Install the macOS menu bar app?", default="yes")
    if choice is None:
        raise SystemExit(0)
    return choice == "yes"


def _resolve_stop_hotkey(args: argparse.Namespace, *, menubar_enabled: bool) -> str | None:
    """Resolve the stop-speaking hotkey for setup.

    Returns a spec, ``"off"``, or ``None`` (leave the config default in place).
    Prompts only when a menu bar is being installed on an interactive Mac — the
    hotkey only works while that app runs.
    """
    if getattr(args, "hotkey", None) is not None:
        return args.hotkey
    if not menubar_enabled or sys.platform != "darwin" or not _interactive():
        return None

    choices = [
        Choice(spec, format_hotkey_display(spec), _HOTKEY_PRESET_HINTS.get(spec, ""))
        for spec in HOTKEY_PRESETS
    ]
    choices.append(Choice("off", "Off", "No global stop-speaking hotkey"))
    choice = select_one(
        choices,
        title="Stop-speaking hotkey",
        subtitle="Press it in any app to silence the current announcement",
        default=DEFAULT_STOP_SPEAKING_HOTKEY,
    )
    if choice is None:
        raise SystemExit(0)
    return choice


def _apply_stop_hotkey(config_path: Path, choice: str | None) -> None:
    """Persist a setup hotkey choice (spec / 'off' / None=no change) and report it."""
    if choice is None:
        return
    if choice.strip().lower() in _HOTKEY_OFF_TOKENS:
        set_hotkey_config(config_path, enabled=False)
        print("✓ Stop-speaking hotkey: off")
        return
    try:
        set_hotkey_config(config_path, enabled=True, stop_speaking=choice)
    except ValueError as exc:
        raise SystemExit(f"Invalid hotkey '{choice}': {exc}")
    print(f"✓ Stop-speaking hotkey: {format_hotkey_display(choice)} (works while the menu bar app runs)")


def _cocoa_available() -> bool:
    try:
        import AppKit  # noqa: F401  - provided by pyobjc-framework-Cocoa
    except Exception:
        return False
    return True


def _menubar_install_command() -> list[str]:
    prefix = Path(sys.prefix)
    is_pipx_venv = prefix.parent.name == "venvs" and prefix.parent.parent.name == "pipx"
    if is_pipx_venv and shutil.which("pipx"):
        return ["pipx", "inject", prefix.name, "pyobjc-framework-Cocoa"]
    return [sys.executable, "-m", "pip", "install", "pyobjc-framework-Cocoa"]


def _ensure_menubar_dependency() -> bool:
    if _cocoa_available():
        return True
    command = _menubar_install_command()
    print(f"  Installing menu bar dependency ({' '.join(command)})…")
    try:
        completed = subprocess.run(command)
    except OSError as exc:
        print(f"! Could not run the installer: {exc}")
        return False
    return completed.returncode == 0


def _maybe_setup_menubar(config: AgentVoiceConfig, *, choice: bool | None) -> None:
    # `choice` is resolved up front by _resolve_menubar_choice: True/False from a
    # flag or prompt, or None to skip (non-macOS or non-interactive).
    if sys.platform != "darwin":
        if choice:
            print("! Menu bar app is macOS-only; skipping.")
        return
    if not choice:
        return
    if not _ensure_menubar_dependency():
        print(
            "! Menu bar dependency install failed. Run "
            "`pipx inject voiccce pyobjc-framework-Cocoa` (or `pip install pyobjc-framework-Cocoa`), "
            "then `voiccce menubar-start`."
        )
        return
    try:
        pid = start_menubar(config)
    except RuntimeError as exc:
        print(f"! Menu bar could not start: {exc}")
        return
    print(f"✓ Menu bar started (pid {pid})")


def cmd_status(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    conn = connect(config.database_path)
    try:
        init_db(conn)
        counts = dict(
            conn.execute(
                "SELECT status, COUNT(*) AS count FROM events GROUP BY status"
            ).fetchall()
        )
        failed = counts.get("failed", 0)
        pending = counts.get("pending", 0)
        processed = counts.get("processed", 0)
        usage_stats = fetch_usage_stats(conn)
    finally:
        conn.close()
    print("Voiccce")
    print(f"Database: {config.database_path}")
    print(f"Queue pending: {pending}")
    print(f"Queue processed: {processed}")
    print(f"Queue failed: {failed}")
    print(f"Language: {language_display_name(config.language)}")
    print(f"Voice: {config.voice_backend} / {config.voice_name or '-'}")
    hotkey_line = format_hotkey_display(config.hotkey_stop_speaking) if config.hotkey_enabled else "off"
    print(f"Stop-speaking hotkey: {hotkey_line} (menu bar)")
    mute_status = voice_mute_status(config)
    if mute_status.muted and mute_status.muted_until:
        muted_until = datetime.fromtimestamp(mute_status.muted_until).strftime("%Y-%m-%d %H:%M:%S")
        print(f"Voice muted until: {muted_until}")
    if config.voice_backend == "openai_tts":
        status = get_openai_secret_status(config)
        print(f"Voice API key: {status.source if status.available else 'missing'}")
    print(
        "Audio generated: "
        f"{usage_stats.audio_generated_count} "
        f"({format_duration(usage_stats.audio_duration_seconds)}, "
        f"{format_usd(usage_stats.audio_cost_usd)} est.)"
    )
    if usage_stats.audio_input_text_tokens or usage_stats.audio_output_audio_tokens:
        print(
            "Audio estimate: "
            f"{usage_stats.audio_input_text_tokens} text tokens, "
            f"{usage_stats.audio_output_audio_tokens} audio tokens est. "
            f"({format_usd(usage_stats.audio_input_cost_usd)} input, "
            f"{format_usd(usage_stats.audio_output_cost_usd)} output)"
        )
    if usage_stats.audio_billed_count:
        print(f"Audio billed: {format_usd(usage_stats.audio_billed_cost_usd)}")
    print(f"Summaries cost: {format_usd(usage_stats.summary_cost_usd)}")
    print(f"Reports listened: {usage_stats.reports_listened_count}")
    print("Adapters: claude-code, codex collectors available")
    summary_status = "disabled"
    if config.summary_enabled:
        summary_status = f"{config.summary_provider} / {config.summary_model}"
    print(f"Summary: {summary_status}")
    pid, running = daemon_status(config)
    print(f"Daemon: {'running' if running else 'stopped'}" + (f" (pid {pid})" if pid else ""))
    menu_pid, menu_running = menubar_status(config)
    print(f"Menu bar: {'running' if menu_running else 'stopped'}" + (f" (pid {menu_pid})" if menu_pid else ""))


def cmd_daemon(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    run_daemon(config, once=args.once, deliver=not args.no_deliver, terminal_only=args.terminal_only)


def cmd_start(args: argparse.Namespace) -> None:
    config_path = write_default_config(args.config)
    config = load_config(config_path)
    pid = start_daemon(config)
    paths = service_paths(config)
    print(f"Daemon running: pid {pid}")
    print(f"Log: {paths.log_path}")


def cmd_stop(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    pid = stop_daemon(config)
    if pid:
        print(f"Daemon stopped: pid {pid}")
    else:
        print("Daemon was not running")


def cmd_update(args: argparse.Namespace) -> None:
    source = _resolve_update_source(args.source)
    config = load_config(args.config)
    daemon_pid, daemon_running = daemon_status(config)
    menubar_pid, menubar_running = menubar_status(config)
    command = _update_install_command(source)

    print(f"Updating Voiccce from: {source}")
    completed = subprocess.run(command, cwd=str(source))
    if completed.returncode != 0:
        raise SystemExit(f"Update failed with exit code {completed.returncode}.")
    print("✓ Package updated")

    if args.no_restart:
        if daemon_running or menubar_running:
            print("Skipped restart; running processes still use the old code until restarted.")
        return

    if daemon_running:
        stopped = stop_daemon(config)
        restarted = start_daemon(config)
        print(f"✓ Daemon restarted ({stopped or daemon_pid} → {restarted})")
    if menubar_running:
        stopped = stop_menubar(config)
        restarted = start_menubar(config)
        print(f"✓ Menu bar restarted ({stopped or menubar_pid} → {restarted})")
    if not daemon_running and not menubar_running:
        print("No running daemon/menu bar to restart.")


def _resolve_update_source(source: str | None) -> Path:
    if source:
        return _validate_update_source(Path(source).expanduser())

    cwd = Path.cwd()
    if _is_update_source(cwd):
        return cwd.resolve()

    installed_source = _installed_source_path()
    if installed_source is not None and _is_update_source(installed_source):
        return installed_source.resolve()

    raise SystemExit(
        "Could not find a local voiccce checkout. Run `voiccce update` from the repo "
        "or pass `--source /path/to/voiccce`."
    )


def _validate_update_source(source: Path) -> Path:
    resolved = source.resolve()
    if not _is_update_source(resolved):
        raise SystemExit(
            f"{resolved} does not look like a voiccce checkout "
            "(expected pyproject.toml and agent_voice/)."
        )
    return resolved


def _is_update_source(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (path / "agent_voice").is_dir()


def _installed_source_path() -> Path | None:
    try:
        direct_url = metadata.distribution("voiccce").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not direct_url:
        return None
    try:
        data = json.loads(direct_url)
    except json.JSONDecodeError:
        return None
    url = str(data.get("url", ""))
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path)).expanduser()


def _update_install_command(source: Path) -> list[str]:
    pip_args = [
        "install",
        "--force-reinstall",
        "--no-deps",
        "-e",
        str(source),
    ]
    pipx_package = _pipx_package_name()
    if pipx_package and shutil.which("pipx"):
        return ["pipx", "runpip", pipx_package, *pip_args]
    return [sys.executable, "-m", "pip", *pip_args]


def _pipx_package_name() -> str | None:
    prefix = Path(sys.prefix)
    package = _pipx_package_name_from_venv(prefix)
    if package:
        return package
    script = shutil.which("voiccce")
    if not script:
        return None
    return _pipx_package_name_from_script(Path(script))


def _pipx_package_name_from_venv(path: Path) -> str | None:
    if path.parent.name == "venvs" and path.parent.parent.name == "pipx":
        return path.name
    return None


def _pipx_package_name_from_script(path: Path) -> str | None:
    resolved = path.resolve()
    if resolved.parent.name != "bin":
        return None
    return _pipx_package_name_from_venv(resolved.parent.parent)


def cmd_menubar(args: argparse.Namespace) -> None:
    from .menubar import run_menubar

    run_menubar(args.config)


def cmd_menubar_start(args: argparse.Namespace) -> None:
    config_path = write_default_config(args.config)
    config = load_config(config_path)
    pid = start_menubar(config)
    paths = menubar_service_paths(config)
    print(f"Menu bar running: pid {pid}")
    print(f"Log: {paths.log_path}")


def cmd_menubar_stop(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    pid = stop_menubar(config)
    if pid:
        print(f"Menu bar stopped: pid {pid}")
    else:
        print("Menu bar was not running")


def cmd_menubar_status(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    pid, running = menubar_status(config)
    print(f"Menu bar: {'running' if running else 'stopped'}" + (f" (pid {pid})" if pid else ""))


def cmd_stop_speaking(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    pid = stop_speaking(config)
    if pid:
        print(f"Stopped voice playback: pid {pid}")
    else:
        print("No active voice playback")


def cmd_mute(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    duration_seconds = parse_duration_seconds(args.duration)
    muted_until = set_voice_mute(config, duration_seconds)
    print(f"Voice muted until: {datetime.fromtimestamp(muted_until).strftime('%Y-%m-%d %H:%M:%S')}")


def cmd_unmute(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    clear_voice_mute(config)
    print("Voice unmuted")


def cmd_test(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    DeliveryRouter(config, terminal_only=args.terminal_only).deliver(test_message(config))


def cmd_config(args: argparse.Namespace) -> None:
    changed = False
    if args.language:
        try:
            config_path = set_config_language(args.config, args.language)
        except ValueError as exc:
            raise SystemExit(str(exc))
        changed = True
    else:
        config_path = write_default_config(args.config)

    voice_options = {
        "backend": args.voice_backend,
        "voice": args.voice,
        "rate": args.voice_rate,
        "speed": args.voice_speed,
        "model": args.voice_model,
        "audio_format": args.voice_format,
        "estimated_cost_per_minute_usd": args.voice_estimated_cost_per_minute,
        "text_input_price_per_million_tokens_usd": args.voice_text_input_price_per_million,
        "audio_output_price_per_million_tokens_usd": args.voice_audio_output_price_per_million,
        "audio_tokens_per_second": args.voice_audio_tokens_per_second,
        "instructions": args.voice_instructions,
        "api_key_env": args.voice_api_key_env,
    }
    if any(value is not None for value in voice_options.values()):
        config_path = set_voice_config(config_path, **voice_options)
        changed = True

    if args.hotkey is not None:
        if args.hotkey.strip().lower() in _HOTKEY_OFF_TOKENS:
            config_path = set_hotkey_config(config_path, enabled=False)
        else:
            try:
                config_path = set_hotkey_config(config_path, enabled=True, stop_speaking=args.hotkey)
            except ValueError as exc:
                raise SystemExit(f"Invalid hotkey '{args.hotkey}': {exc}")
        changed = True

    config = load_config(args.config)
    normalize_language(config.language)
    print(f"Config: {config.config_path}")
    print(f"Language: {language_display_name(config.language)}")
    print(f"Database: {config.database_path}")
    print(f"Voice backend: {config.voice_backend}")
    print(f"Voice: {config.voice_name or '-'}")
    print(f"Voice speed: {config.voice_speed:g}")
    print(f"Voice model: {config.voice_model}")
    print(f"Voice format: {config.voice_format}")
    print(f"Voice estimated cost per minute: {format_usd(config.voice_estimated_cost_per_minute_usd)}")
    print(f"Voice text input price per 1M tokens: {format_usd(config.voice_text_input_price_per_million_tokens_usd)}")
    print(f"Voice audio output price per 1M tokens: {format_usd(config.voice_audio_output_price_per_million_tokens_usd)}")
    print(f"Voice audio tokens per second estimate: {config.voice_audio_tokens_per_second:g}")
    print(f"Voice API key env: {config.voice_api_key_env}")
    hotkey_line = format_hotkey_display(config.hotkey_stop_speaking) if config.hotkey_enabled else "off"
    print(f"Stop-speaking hotkey: {hotkey_line}")
    print(f"Summary: {'enabled' if config.summary_enabled else 'disabled'}")
    print(f"Summary provider: {config.summary_provider}")
    print(f"Summary model: {config.summary_model}")
    print(f"Summary privacy: {config.summary_privacy_level}")
    print(f"Summary max input chars: {config.summary_max_input_chars}")
    print(f"Summary max words: {config.summary_max_words}")
    print(f"Summary timeout: {config.summary_timeout_seconds}s")
    print(f"Summary text input price per 1M tokens: {format_usd(config.summary_text_input_price_per_million_tokens_usd)}")
    print(f"Summary cached input price per 1M tokens: {format_usd(config.summary_cached_input_price_per_million_tokens_usd)}")
    print(f"Summary text output price per 1M tokens: {format_usd(config.summary_text_output_price_per_million_tokens_usd)}")
    status = get_openai_secret_status(config)
    print(f"Voice API key status: {status.source if status.available else 'missing'}")
    if changed:
        print("Updated config. Restart daemon to apply changes.")


def cmd_events(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    conn = connect(config.database_path)
    try:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT event_key, agent_name, event_type, project_name, status, created_at
            FROM events
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        print(
            f"{row['created_at']} {row['status']} {row['agent_name']} "
            f"{row['event_type']} {row['project_name'] or '-'} {row['event_key']}"
        )


def cmd_collect(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.hook == "UserPromptSubmit":
        _handle_user_activity(config)
        return
    conn = connect(config.database_path)
    try:
        if args.agent == "codex":
            event = read_codex_event_from_stdin(args.hook)
        elif args.agent == "pi":
            event = read_pi_event_from_stdin(args.hook)
        else:
            event = read_claude_event_from_stdin(args.hook)
        result = enqueue_event(conn, event)
    except (json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"voiccce collect failed: {exc}", file=sys.stderr)
        return
    finally:
        conn.close()
    print(json.dumps({"inserted": result.inserted, "event_key": result.event_key}, ensure_ascii=False))


def _handle_user_activity(config: AgentVoiceConfig) -> None:
    """Stop a playing announcement when the user replies into that same session."""
    if not config.voice_interrupt_on_user_input:
        return
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    session_id = (
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("conversation_id")
        or payload.get("run_id")
    )
    if not session_id or not voice_session_active(config, str(session_id)):
        return
    pid = stop_speaking(config)
    print(json.dumps({"interrupted": True, "session_id": session_id, "pid": pid}, ensure_ascii=False))


def cmd_enqueue_test_event(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    conn = connect(config.database_path)
    try:
        event = NormalizedEvent.build(
            agent_name="codex",
            event_type=args.type,
            project_name=args.project,
            session_id=args.session,
            ask_summary=args.ask,
        )
        result = enqueue_event(conn, event)
    finally:
        conn.close()
    print(json.dumps({"inserted": result.inserted, "event_key": result.event_key}, ensure_ascii=False))


def cmd_secret_set(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.name == "openai":
        secret = getpass.getpass("OpenAI API key: ").strip()
        if not secret:
            raise SystemExit("No key entered")
        validation = _validate_openai_key(config, secret)
        if not validation.ok:
            raise SystemExit(f"OpenAI key validation failed: {validation.error}")
        set_openai_keychain_secret(config, secret)
        print("OpenAI API key stored in macOS Keychain.")
        print("Restart daemon to apply changes.")


def cmd_secret_status(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.name == "openai":
        status = get_openai_secret_status(config)
        print(f"OpenAI API key: {status.source if status.available else 'missing'}")


def cmd_secret_delete(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.name == "openai":
        deleted = delete_openai_keychain_secret(config)
        print("OpenAI API key deleted from macOS Keychain." if deleted else "OpenAI API key was not in Keychain.")


if __name__ == "__main__":
    main()
