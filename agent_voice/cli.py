from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote, urlparse

from . import _resolve_version
from .config import (
    AgentVoiceConfig,
    ConfigError,
    SUMMARY_PRIVACY_LEVELS,
    language_display_name,
    list_config_backups,
    load_config,
    normalize_language,
    reset_config,
    restore_config_backup,
    set_autostart_managed,
    set_reminders_config,
    set_config_language,
    set_daemon_config,
    set_events_config,
    set_hotkey_config,
    set_limits_config,
    set_quiet_hours_config,
    set_summary_config,
    set_voice_config,
    write_default_config,
)
from .hotkey import DEFAULT_STOP_SPEAKING_HOTKEY, HOTKEY_PRESETS, format_hotkey_display
from .daemon import in_quiet_hours, process_once, run_daemon
from .db import (
    cancel_reminder,
    clear_events,
    clear_notifications,
    clear_reminders,
    clear_session_states,
    connect,
    db_size_bytes,
    init_db,
    prune_processed_events,
    vacuum_db,
)
from .delivery import DeliveryRouter, test_message
from .doctor import (
    CheckResult,
    doctor_ok,
    inspect_agent_wiring,
    run_doctor,
)
from .hooks.claude_event_collector import read_event_from_stdin as read_claude_event_from_stdin
from .hooks.codex_event_collector import read_event_from_stdin as read_codex_event_from_stdin
from .hooks.pi_event_collector import read_event_from_stdin as read_pi_event_from_stdin
from .installer import WrapperImportError
from .installer.claude_code import install_claude_code_personal, remove_claude_code_personal
from .installer.codex import install_codex_personal, remove_codex_personal
from .installer.pi import install_pi_personal, remove_pi_personal
from .intelligence.pipeline_log import pipeline_log_path, truncate_pipeline_log
from .launchagent import (
    DAEMON_LABEL,
    MENUBAR_LABEL,
    autostart_status,
    disable_autostart,
    enable_autostart,
    plist_path,
)
from .models import EventType, NormalizedEvent
from .queue import enqueue_event
from .runtime import (
    clear_voice_mute,
    parse_age_seconds,
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
from .teardown import (
    TeardownPlan,
    TeardownReport,
    detect_wired_integrations,
    run_teardown,
)
from .ui import Choice, checkbox_select, confirm, select_one
from .service import (
    daemon_status,
    menubar_service_paths,
    menubar_status,
    service_paths,
    stale_pid_warnings,
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

# Remote install/update source used when no local checkout is present and no
# --source is given. ``DEFAULT_UPDATE_REF`` is the git ref pinned by default.
REPO_GIT_URL = "git+https://github.com/blackbalancef/voiccce"
DEFAULT_UPDATE_REF = "main"

# Toggle tokens accepted by on/off-style config flags.
_ON_TOKENS = {"on", "true", "yes", "1", "enable", "enabled"}
_OFF_TOKENS = {"off", "false", "no", "0", "disable", "disabled"}

# Event names accepted by `voiccce config --event NAME=on|off`, mapped to the
# set_events_config keyword. These mirror agent_voice.config.set_events_config.
_EVENT_FLAG_NAMES = (
    "task_finished",
    "permission_needed",
    "input_needed",
    "task_failed",
    "subagent_finished",
)

# Which integrations `voiccce uninstall <target>` can unwire on its own, mapped
# to the module-level name of the installer remover that strips that
# integration's hooks + wrapper. Names (not the functions) are stored so the
# handler resolves them via the module namespace at call time and stays
# patchable in tests.
_UNINSTALL_REMOVER_NAMES: dict[str, str] = {
    "claude-code": "remove_claude_code_personal",
    "codex": "remove_codex_personal",
    "pi": "remove_pi_personal",
}

# Installers used to RE-APPLY hooks after a successful update, keyed by the names
# teardown.detect_wired_integrations returns; values are module-level names,
# resolved at call time so tests can patch them.
_REAPPLY_INSTALLER_NAMES: dict[str, str] = {
    "claude-code": "install_claude_code_personal",
    "codex": "install_codex_personal",
    "pi": "install_pi_personal",
}

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
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"voiccce {_resolve_version()}",
        help="Show the installed Voiccce version and exit",
    )
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

    update = subparsers.add_parser("update", help="Update this installation (local checkout or git)")
    update.add_argument(
        "--source",
        help="Path to the voiccce checkout. Defaults to the current directory or the original install source.",
    )
    update.add_argument(
        "--ref",
        default=DEFAULT_UPDATE_REF,
        help=f"Git ref to install when updating from {REPO_GIT_URL} (default: {DEFAULT_UPDATE_REF}).",
    )
    update.add_argument(
        "--dev",
        action="store_true",
        help="Install the source checkout in editable mode (-e) for local development.",
    )
    update.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart daemon/menu bar after updating.",
    )
    update.add_argument(
        "--no-hooks",
        action="store_true",
        help="Do not re-apply hooks for wired integrations after updating.",
    )
    update.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip the post-update health probe (enqueue + process a test event).",
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
    config_cmd.add_argument("--summary", choices=["on", "off"], help="Enable or disable AI summaries")
    config_cmd.add_argument(
        "--summary-privacy",
        choices=list(SUMMARY_PRIVACY_LEVELS),
        help="How much of the assistant's last message summaries may send",
    )
    config_cmd.add_argument("--summary-model", help="Summary model, e.g. gpt-5.4-nano")
    config_cmd.add_argument("--summary-provider", help="Summary provider, e.g. openai or fallback")
    config_cmd.add_argument(
        "--summary-pipeline-log",
        choices=["on", "off"],
        help="Write the summary pipeline log (summary.log)",
    )
    config_cmd.add_argument(
        "--event",
        dest="events",
        action="append",
        metavar="NAME=on|off",
        help=(
            "Toggle an event notification, e.g. --event subagent_finished=on. "
            "Names: " + ", ".join(_EVENT_FLAG_NAMES) + ". Repeatable."
        ),
    )
    config_cmd.add_argument("--max-events-per-minute", type=int, help="Rate-limit notifications per minute")
    config_cmd.add_argument("--daily-spend-cap", type=float, help="Daily spend cap in USD (0 = no cap)")
    config_cmd.add_argument("--monthly-spend-cap", type=float, help="Monthly spend cap in USD (0 = no cap)")
    config_cmd.add_argument("--event-retention-days", type=int, help="Days to keep processed events (0 = forever)")
    config_cmd.add_argument(
        "--interrupt-on-reply",
        choices=["on", "off"],
        help="Stop the current announcement when you reply into that session",
    )
    config_cmd.add_argument(
        "--quiet-hours",
        choices=["on", "off"],
        help="Enable or disable the nightly quiet-hours window",
    )
    config_cmd.add_argument("--quiet-hours-from", help="Quiet-hours start, HH:MM (e.g. 23:00)")
    config_cmd.add_argument("--quiet-hours-to", help="Quiet-hours end, HH:MM (e.g. 09:00)")
    config_cmd.add_argument("--quiet-hours-voice", choices=["on", "off"], help="Allow voice during quiet hours")
    config_cmd.add_argument("--quiet-hours-desktop", choices=["on", "off"], help="Allow desktop notifications during quiet hours")
    config_cmd.add_argument("--idle-reminders", choices=["on", "off"], help="Speak a short 'still waiting' nudge before the agent's cache expires")
    config_cmd.add_argument("--idle-reminder-margin", type=int, metavar="MIN", help="Minutes before cache expiry to nudge (default 1)")
    config_cmd.add_argument("--reset", action="store_true", help="Reset config to defaults (a backup is written)")
    config_cmd.add_argument("--reset-section", help="Only reset this section (use with --reset), e.g. summary")
    config_cmd.add_argument("--list-backups", action="store_true", help="List config backups (newest first) and exit")
    config_cmd.add_argument(
        "--restore",
        nargs="?",
        const="",
        metavar="BACKUP",
        help="Restore config from a backup (newest if no path given); the current file is backed up first",
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

    doctor = subparsers.add_parser("doctor", help="Run health checks on the installation")
    doctor.add_argument(
        "--no-validate-key",
        action="store_true",
        help="Skip the live OpenAI key validation (offline/CI friendly)",
    )
    doctor.add_argument("--json", action="store_true", dest="as_json", help="Print machine-readable JSON")
    doctor.set_defaults(handler=cmd_doctor)

    logs = subparsers.add_parser("logs", help="Tail a Voiccce log file")
    logs_source = logs.add_mutually_exclusive_group()
    logs_source.add_argument("--daemon", action="store_const", const="daemon", dest="log_source", help="Daemon log (default)")
    logs_source.add_argument("--menubar", action="store_const", const="menubar", dest="log_source", help="Menu bar log")
    logs_source.add_argument("--hook", action="store_const", const="hook", dest="log_source", help="Hook log")
    logs_source.add_argument("--summary", action="store_const", const="summary", dest="log_source", help="Summary pipeline log")
    logs.add_argument("-n", type=int, default=50, dest="lines", help="Number of lines to show (default 50)")
    logs.add_argument("-f", action="store_true", dest="follow", help="Follow the log (best-effort tail -f)")
    logs.set_defaults(handler=cmd_logs, log_source="daemon")

    prune = subparsers.add_parser("prune", help="Delete old processed events and reclaim space")
    prune.add_argument(
        "--older-than",
        dest="older_than",
        help="Age cutoff like 30d, 12h, 90m (bare number = seconds). "
        "Defaults to the configured event retention.",
    )
    prune.set_defaults(handler=cmd_prune)

    clear = subparsers.add_parser("clear", help="Clear queued events and/or notification history")
    clear.add_argument("--events", action="store_true", help="Clear queued/processed events")
    clear.add_argument("--history", action="store_true", help="Clear notification + session history and pipeline log")
    clear.add_argument("--all", action="store_true", dest="clear_all", help="Clear events and history")
    clear.add_argument("--yes", action="store_true", help="Do not prompt for confirmation")
    clear.set_defaults(handler=cmd_clear)

    autostart = subparsers.add_parser("autostart", help="Manage macOS login autostart (launchd)")
    autostart_sub = autostart.add_subparsers(dest="autostart_command")
    autostart_enable = autostart_sub.add_parser("enable", help="Install and load the autostart agents")
    autostart_enable.set_defaults(handler=cmd_autostart_enable)
    autostart_disable = autostart_sub.add_parser("disable", help="Unload and remove the autostart agents")
    autostart_disable.set_defaults(handler=cmd_autostart_disable)
    autostart_status_cmd = autostart_sub.add_parser("status", help="Show autostart agent status")
    autostart_status_cmd.set_defaults(handler=cmd_autostart_status)

    uninstall = subparsers.add_parser(
        "uninstall",
        help="Remove one integration's hooks, or tear down everything",
    )
    uninstall.add_argument(
        "target",
        nargs="?",
        choices=["claude-code", "codex", "pi"],
        help="Only unwire this integration. Omit to tear down the whole install.",
    )
    uninstall.add_argument("--purge", action="store_true", help="Also delete ~/.voiccce (off by default)")
    uninstall.add_argument(
        "--restore-backups",
        action="store_true",
        help="Restore each integration's most recent pre-install backup",
    )
    uninstall.add_argument("--yes", action="store_true", help="Do not prompt for confirmation")
    uninstall.set_defaults(handler=cmd_uninstall)

    # Internal commands invoked by the daemon launcher and generated hook
    # wrappers, not by users: omitting ``help`` hides them from the help body
    # (they remain reachable in the choices metavar).
    daemon = subparsers.add_parser("daemon")  # internal
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

    collect = subparsers.add_parser("collect")  # internal
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

    enqueue_test = subparsers.add_parser("enqueue-test-event")  # internal
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


def _warn_non_macos() -> None:
    """Print a one-line warning when not on macOS, where voice playback works."""
    if sys.platform != "darwin":
        print("! Voiccce targets macOS; voice needs say/afplay, which are macOS tools.")


def cmd_install(args: argparse.Namespace) -> None:
    try:
        _cmd_install(args)
    except WrapperImportError as exc:
        raise SystemExit(str(exc))


def _cmd_install(args: argparse.Namespace) -> None:
    _warn_non_macos()
    # Honor --config so `install --config X` writes config/db to X (like `setup`).
    config_kwargs: dict[str, Path] = (
        {"config_path": Path(args.config).expanduser()} if args.config else {}
    )
    if args.target == "claude-code":
        result = install_claude_code_personal(verify=True, **config_kwargs, **_claude_install_kwargs(args))
        print(f"Claude Code personal settings: {result.settings_path}")
        print(f"Backup: {result.backup_path}")
        print(f"Hook wrapper: {result.wrapper_path}")
        print(f"Config: {result.config_path}")
        print(f"Database: {result.database_path}")
        print("Installed hooks: " + ", ".join(result.installed_events))
        return

    if args.target == "codex":
        result = install_codex_personal(verify=True, **config_kwargs, **_codex_install_kwargs(args))
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
        result = install_pi_personal(verify=True, **config_kwargs, **_pi_install_kwargs(args))
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

    _warn_non_macos()

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

    print(
        "Privacy: openai_tts summaries send the assistant's last message to OpenAI; "
        "limit this with `voiccce config --summary-privacy metadata_only`."
    )

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
        # Use the localized test phrase so the spoken test honors the configured
        # language (e.g. Russian), not a hardcoded English string.
        results = DeliveryRouter(config).deliver(test_message(config))
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


def format_bytes(num_bytes: int) -> str:
    """Render a byte count as a short human-readable size (e.g. ``1.2 MB``)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover - unreachable, loop returns first


def _last_delivered_at(conn: sqlite3.Connection) -> int | None:
    """Epoch seconds of the most recently delivered notification, or ``None``."""
    row = conn.execute(
        "SELECT MAX(delivered_at) FROM notifications WHERE delivered_at IS NOT NULL"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


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
        last_delivered = _last_delivered_at(conn)
    finally:
        conn.close()
    print("Voiccce")
    print(f"Version: {_resolve_version()}")
    print(f"Database: {config.database_path} ({format_bytes(db_size_bytes(config.database_path))})")
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
    if last_delivered is not None:
        delivered_at = datetime.fromtimestamp(last_delivered).strftime("%Y-%m-%d %H:%M:%S")
        print(f"Last delivered: {delivered_at}")
    else:
        print("Last delivered: never")
    print("Agents:")
    for wiring in inspect_agent_wiring(config):
        if wiring.wired:
            events = ", ".join(wiring.events) if wiring.events else "-"
            print(f"  {wiring.agent}: wired ({events})")
        else:
            print(f"  {wiring.agent}: not wired ({wiring.detail})")
    summary_status = "disabled"
    if config.summary_enabled:
        summary_status = f"{config.summary_provider} / {config.summary_model}"
    print(f"Summary: {summary_status}")
    window = f"{config.quiet_hours_from}-{config.quiet_hours_to}"
    if not config.quiet_hours_enabled:
        print(f"Quiet hours: disabled ({window})")
    elif in_quiet_hours(config):
        print(f"Quiet hours: active ({window})")
    else:
        print(f"Quiet hours: enabled ({window}, not active now)")
    pid, running = daemon_status(config)
    print(f"Daemon: {'running' if running else 'stopped'}" + (f" (pid {pid})" if pid else ""))
    menu_pid, menu_running = menubar_status(config)
    print(f"Menu bar: {'running' if menu_running else 'stopped'}" + (f" (pid {menu_pid})" if menu_pid else ""))
    for label, stale_pid in stale_pid_warnings(config):
        print(f"! Stale pid: {label} pid {stale_pid} is recorded but not running")
    if config.autostart_managed:
        status = autostart_status(config)
        daemon_loaded = status.get(DAEMON_LABEL, {}).get("loaded")
        menubar_loaded = status.get(MENUBAR_LABEL, {}).get("loaded")
        print(
            "Autostart: managed "
            f"(daemon {'loaded' if daemon_loaded else 'not loaded'}, "
            f"menu bar {'loaded' if menubar_loaded else 'not loaded'})"
        )


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
    config = load_config(args.config)
    daemon_pid, daemon_running = daemon_status(config)
    menubar_pid, menubar_running = menubar_status(config)

    source = _resolve_update_source(args.source)
    if source is not None:
        target = str(source)
        cwd = str(source)
        print(f"Updating Voiccce from: {source}")
    else:
        target = f"{REPO_GIT_URL}@{args.ref}"
        cwd = None
        print(f"Updating Voiccce from: {target}")

    before_version = _resolve_version()
    editable = bool(args.dev) and source is not None
    use_pipx = _update_uses_pipx(editable=editable)
    # Capture injected extras (e.g. the menu bar's pyobjc) before the venv is
    # recreated, so they can be re-injected afterward.
    extras = _installed_optional_extras() if use_pipx else []
    command = _update_install_command(target, editable=editable)
    env = _install_env() if use_pipx else None
    completed = subprocess.run(command, cwd=cwd, env=env)
    if completed.returncode != 0:
        raise SystemExit(f"Update failed with exit code {completed.returncode}.")
    if extras:
        _reinject_extras(extras, env=env)
    after_version = _resolve_version()
    if before_version == after_version:
        print(f"✓ Package updated (already up to date at {after_version})")
    else:
        print(f"✓ Package updated ({before_version} → {after_version})")

    if not args.no_hooks:
        _reapply_wired_hooks(config)

    if not args.no_probe:
        _health_probe(config)

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


def _reapply_wired_hooks(config: AgentVoiceConfig) -> None:
    """Regenerate hook wrappers for every currently-wired integration.

    After a package upgrade the generated wrappers may reference a stale path or
    interpreter; re-running the installer for each wired agent regenerates them.
    Best-effort: a failure for one agent is reported but never aborts the update.
    """
    wired = detect_wired_integrations(config)
    if not wired:
        print("No wired integrations to re-apply hooks for.")
        return
    for target in wired:
        installer_name = _REAPPLY_INSTALLER_NAMES.get(target)
        if installer_name is None:  # pragma: no cover - detect_* only returns known names
            continue
        installer = globals()[installer_name]
        try:
            installer(config_path=config.config_path, verify=True)
        except Exception as exc:
            print(f"! Could not re-apply {target} hooks: {exc}")
            continue
        print(f"✓ Re-applied {target} hooks")


def _health_probe(config: AgentVoiceConfig) -> None:
    """Enqueue and process one synthetic event to confirm the pipeline still works."""
    conn = connect(config.database_path)
    try:
        init_db(conn)
        event = NormalizedEvent.build(
            agent_name="codex",
            event_type=EventType.TASK_FINISHED.value,
            project_name="voiccce",
            session_id=f"update-probe-{int(time.time())}",
        )
        enqueue_event(conn, event)
        result = process_once(conn, config, deliver=False)
    except Exception as exc:
        print(f"! Health probe failed: {exc}; check `voiccce doctor`.")
        return
    finally:
        conn.close()
    if result.processed_events:
        print("✓ Health probe processed a test event")
    else:
        print("! Health probe did not process the test event; check `voiccce doctor`.")

    _, daemon_running = daemon_status(config)
    if not daemon_running and not stale_pid_warnings(config):
        return
    for label, stale_pid in stale_pid_warnings(config):
        print(f"! Service {label} did not recover (stale pid {stale_pid}); run `voiccce start`.")


def _resolve_update_source(source: str | None) -> Path | None:
    """Resolve the local checkout to update from, or ``None`` to use the git URL.

    An explicit ``--source`` must be a valid checkout (raises otherwise). With no
    flag, prefer the current directory, then the recorded install source; when
    neither is a checkout, return ``None`` so the caller updates from git.
    """
    if source:
        return _validate_update_source(Path(source).expanduser())

    cwd = Path.cwd()
    if _is_update_source(cwd):
        return cwd.resolve()

    installed_source = _installed_source_path()
    if installed_source is not None and _is_update_source(installed_source):
        return installed_source.resolve()

    return None


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


# Optional extras (pip distribution names) that `voiccce setup` injects on demand
# rather than declaring as hard dependencies. They live in the pipx venv, so a
# venv recreate drops them and we re-inject after an update.
_OPTIONAL_EXTRA_PACKAGES: tuple[str, ...] = ("pyobjc-framework-Cocoa", "tiktoken")


def _update_uses_pipx(*, editable: bool) -> bool:
    return bool(_pipx_package_name() and shutil.which("pipx") and not editable)


def _update_install_command(target: str, *, editable: bool = False) -> list[str]:
    """Build the install command for ``target`` (a checkout path or git URL spec).

    Prefer ``pipx install --force <target>`` when running inside a pipx-managed
    venv so the app's own venv is reinstalled (non-editable, letting pip resolve
    dependencies). For an editable dev install (``--dev`` on a local checkout) or
    when pipx is unavailable, fall back to ``pip install --force-reinstall``. The
    unconditional ``--no-deps`` was dropped so upgraded dependencies install too.
    """
    if _update_uses_pipx(editable=editable):
        return ["pipx", "install", "--force", target]
    pip_args = ["install", "--force-reinstall"]
    if editable:
        pip_args.append("-e")
    pip_args.append(target)
    pipx_package = _pipx_package_name()
    if pipx_package and shutil.which("pipx"):
        return ["pipx", "runpip", pipx_package, *pip_args]
    return [sys.executable, "-m", "pip", *pip_args]


def _install_env() -> dict[str, str]:
    """Environment for a pipx install that must recreate the venv.

    pipx's uv backend refuses to overwrite an existing venv unless ``UV_VENV_CLEAR``
    is set, so ``pipx install --force`` otherwise fails on a re-run. Setting it makes
    recreate-the-venv the default, which is what an update wants.
    """
    env = os.environ.copy()
    env["UV_VENV_CLEAR"] = "1"
    return env


def _installed_optional_extras() -> list[str]:
    """Return the optional-extra packages currently present in this environment.

    These were injected (e.g. ``pyobjc-framework-Cocoa`` for the menu bar) and are
    lost when the venv is recreated, so an update re-injects them afterward.
    """
    found: list[str] = []
    for name in _OPTIONAL_EXTRA_PACKAGES:
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        found.append(name)
    return found


def _reinject_extras(packages: list[str], *, env: dict[str, str] | None = None) -> None:
    """Re-inject optional extras into the pipx venv after a recreate."""
    pipx_package = _pipx_package_name()
    if not packages or not pipx_package or not shutil.which("pipx"):
        return
    completed = subprocess.run(["pipx", "inject", pipx_package, *packages], env=env)
    if completed.returncode == 0:
        print(f"✓ Re-injected: {', '.join(packages)}")
    else:
        print(
            "! Could not re-inject "
            f"{', '.join(packages)}; the menu bar may need "
            f"`pipx inject {pipx_package} {' '.join(packages)}`."
        )


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
    results = DeliveryRouter(config, terminal_only=args.terminal_only).deliver(test_message(config))
    delivered = next((r for r in results if r.delivered), None)
    if delivered is not None:
        print(f"Test played via {delivered.channel}")
        return
    error = next((r.error for r in results if r.error), None)
    print(f"Test failed: {error or 'no delivery channel succeeded'}")
    hint = _test_remediation_hint(error)
    if hint:
        print(f"Hint: {hint}")
    raise SystemExit(1)


def _test_remediation_hint(error: str | None) -> str | None:
    """Map a delivery error to a one-line remediation hint, or ``None``."""
    if not error:
        return None
    lowered = error.lower()
    if "mute" in lowered:
        return "voice muted -> voiccce unmute"
    if "http 401" in lowered or "invalid" in lowered or "api key" in lowered or "is not set" in lowered:
        return "key invalid -> voiccce secret set openai"
    if "afplay" in lowered:
        return "afplay missing -> install it (ships with macOS); voiccce targets macOS"
    if "say command not found" in lowered:
        return "say missing -> voiccce targets macOS (say ships with macOS)"
    return None


def _toggle_flag(value: str | None, label: str) -> bool | None:
    """Parse an on/off-style flag value into a bool, or ``None`` when unset."""
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in _ON_TOKENS:
        return True
    if lowered in _OFF_TOKENS:
        return False
    raise SystemExit(f"Invalid value for {label}: '{value}'. Use on or off.")


def _parse_event_flags(specs: list[str] | None) -> dict[str, bool]:
    """Parse ``--event NAME=on|off`` specs into ``{name: bool}``; validate names."""
    flags: dict[str, bool] = {}
    for spec in specs or []:
        name, sep, raw = spec.partition("=")
        name = name.strip()
        if not sep:
            raise SystemExit(f"Invalid --event '{spec}'. Use NAME=on or NAME=off.")
        if name not in _EVENT_FLAG_NAMES:
            raise SystemExit(
                f"Unknown event '{name}'. Names: {', '.join(_EVENT_FLAG_NAMES)}."
            )
        toggled = _toggle_flag(raw, f"--event {name}")
        if toggled is not None:
            flags[name] = toggled
    return flags


def cmd_config(args: argparse.Namespace) -> None:
    if args.list_backups:
        backups = list_config_backups(args.config)
        if not backups:
            print("No config backups found.")
            return
        print("Config backups (newest first):")
        for backup in backups:
            print(f"  {backup}")
        return

    if args.restore is not None:
        backup_arg = args.restore or None
        try:
            restored = restore_config_backup(args.config, backup_arg)
        except ConfigError as exc:
            raise SystemExit(str(exc))
        print(f"Restored config from: {restored}")
        print("Restart daemon to apply changes.")
        return

    if args.reset:
        config_path = write_default_config(args.config)
        try:
            backup_path = reset_config(config_path, args.reset_section)
        except ValueError as exc:
            raise SystemExit(str(exc))
        target = f"section [{args.reset_section}]" if args.reset_section else "config"
        print(f"Reset {target} to defaults. Backup: {backup_path}")
        print("Restart daemon to apply changes.")
        return

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

    summary_options: dict[str, object] = {}
    summary_enabled = _toggle_flag(args.summary, "--summary")
    if summary_enabled is not None:
        summary_options["enabled"] = summary_enabled
    if args.summary_privacy is not None:
        summary_options["privacy_level"] = args.summary_privacy
    if args.summary_model is not None:
        summary_options["model"] = args.summary_model
    if args.summary_provider is not None:
        summary_options["provider"] = args.summary_provider
    pipeline_log = _toggle_flag(args.summary_pipeline_log, "--summary-pipeline-log")
    if pipeline_log is not None:
        summary_options["pipeline_log"] = pipeline_log
    if summary_options:
        try:
            config_path = set_summary_config(config_path, **summary_options)
        except (ValueError, ConfigError) as exc:
            raise SystemExit(str(exc))
        changed = True

    event_flags = _parse_event_flags(args.events)
    if event_flags:
        config_path = set_events_config(config_path, **event_flags)
        changed = True

    limits_options: dict[str, object] = {}
    if args.max_events_per_minute is not None:
        limits_options["max_events_per_minute"] = args.max_events_per_minute
    if args.daily_spend_cap is not None:
        limits_options["daily_spend_cap_usd"] = args.daily_spend_cap
    if args.monthly_spend_cap is not None:
        limits_options["monthly_spend_cap_usd"] = args.monthly_spend_cap
    if limits_options:
        try:
            config_path = set_limits_config(config_path, **limits_options)
        except ValueError as exc:
            raise SystemExit(str(exc))
        changed = True

    if args.event_retention_days is not None:
        try:
            config_path = set_daemon_config(config_path, event_retention_days=args.event_retention_days)
        except ValueError as exc:
            raise SystemExit(str(exc))
        changed = True

    interrupt = _toggle_flag(args.interrupt_on_reply, "--interrupt-on-reply")
    if interrupt is not None:
        config_path = set_voice_config(config_path, interrupt_on_user_input=interrupt)
        changed = True

    quiet_options: dict[str, object] = {}
    quiet_enabled = _toggle_flag(args.quiet_hours, "--quiet-hours")
    if quiet_enabled is not None:
        quiet_options["enabled"] = quiet_enabled
    if args.quiet_hours_from is not None:
        quiet_options["start"] = args.quiet_hours_from
    if args.quiet_hours_to is not None:
        quiet_options["end"] = args.quiet_hours_to
    quiet_voice = _toggle_flag(args.quiet_hours_voice, "--quiet-hours-voice")
    if quiet_voice is not None:
        quiet_options["voice"] = quiet_voice
    quiet_desktop = _toggle_flag(args.quiet_hours_desktop, "--quiet-hours-desktop")
    if quiet_desktop is not None:
        quiet_options["desktop"] = quiet_desktop
    if quiet_options:
        try:
            config_path = set_quiet_hours_config(config_path, **quiet_options)
        except ValueError as exc:
            raise SystemExit(str(exc))
        changed = True

    reminder_options: dict[str, object] = {}
    idle_reminders = _toggle_flag(args.idle_reminders, "--idle-reminders")
    if idle_reminders is not None:
        reminder_options["enabled"] = idle_reminders
    if args.idle_reminder_margin is not None:
        reminder_options["margin_minutes"] = args.idle_reminder_margin
    if reminder_options:
        try:
            config_path = set_reminders_config(config_path, **reminder_options)
        except ValueError as exc:
            raise SystemExit(str(exc))
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
    print(f"Summary pipeline log: {'on' if config.summary_pipeline_log else 'off'}")
    print(f"Interrupt on reply: {'on' if config.voice_interrupt_on_user_input else 'off'}")
    if config.quiet_hours_enabled:
        allowed = []
        if config.quiet_hours_voice:
            allowed.append("voice")
        if config.quiet_hours_desktop:
            allowed.append("desktop")
        allow_note = f"; allows {', '.join(allowed)}" if allowed else "; silences voice + desktop"
        quiet = f"{config.quiet_hours_from}-{config.quiet_hours_to}{allow_note}"
    else:
        quiet = "off"
    print(f"Quiet hours: {quiet}")
    print(f"Max events per minute: {config.max_events_per_minute}")
    print(f"Daily spend cap: {format_usd(config.daily_spend_cap_usd) if config.daily_spend_cap_usd else 'none'}")
    print(f"Monthly spend cap: {format_usd(config.monthly_spend_cap_usd) if config.monthly_spend_cap_usd else 'none'}")
    print(f"Event retention days: {config.event_retention_days or 'forever'}")
    if config.idle_reminder_enabled:
        print(f"Idle reminder: on (nudge {config.idle_reminder_margin_minutes} min before cache expiry)")
    else:
        print("Idle reminder: off")
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
    """React to a user reply into a session.

    Always cancels that session's pending idle reminder (the user is back, so there
    is nothing to nudge about). Then, only when interrupt-on-reply is enabled and the
    session is currently being spoken, cuts the playback short.
    """
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
    if not session_id:
        return
    session_id = str(session_id)
    _cancel_session_reminder(config, session_id)
    if not config.voice_interrupt_on_user_input:
        return
    if not voice_session_active(config, session_id):
        return
    pid = stop_speaking(config)
    print(json.dumps({"interrupted": True, "session_id": session_id, "pid": pid}, ensure_ascii=False))


def _cancel_session_reminder(config: AgentVoiceConfig, session_id: str) -> None:
    """Best-effort: drop any pending idle reminder for ``session_id``."""
    try:
        conn = connect(config.database_path)
    except Exception:  # pragma: no cover - defensive
        return
    try:
        init_db(conn)
        cancel_reminder(conn, session_id)
    except Exception:  # pragma: no cover - never let a hook fail the agent
        pass
    finally:
        conn.close()


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


def cmd_doctor(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    results = run_doctor(config, validate_key=not args.no_validate_key)
    if args.as_json:
        print(_doctor_json(results))
    else:
        for result in results:
            mark = "PASS" if result.ok else "FAIL"
            print(f"[{mark}] {result.name}: {result.detail}")
            if not result.ok and result.hint:
                print(f"       hint: {result.hint}")
    if not doctor_ok(results):
        raise SystemExit(1)


def _doctor_json(results: list[CheckResult]) -> str:
    payload = {
        "ok": doctor_ok(results),
        "checks": [
            {"name": r.name, "ok": r.ok, "detail": r.detail, "hint": r.hint}
            for r in results
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def cmd_logs(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    log_path = _log_path_for(config, args.log_source)
    if not log_path.exists() or log_path.stat().st_size == 0:
        print(f"No {args.log_source} log yet at {log_path}.")
        return
    print(f"=== {args.log_source} log: {log_path} ===")
    for line in _tail_lines(log_path, args.lines):
        print(line)
    if args.follow:
        _follow_log(log_path)


def _log_path_for(config: AgentVoiceConfig, source: str) -> Path:
    if source == "menubar":
        return menubar_service_paths(config).log_path
    if source == "hook":
        return config.config_path.parent / "hook.log"
    if source == "summary":
        return pipeline_log_path(config)
    return service_paths(config).log_path


def _tail_lines(path: Path, count: int) -> list[str]:
    """Return the last ``count`` lines of ``path`` (best-effort, full read)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"(could not read log: {exc})"]
    lines = text.splitlines()
    if count <= 0:
        return lines
    return lines[-count:]


def _follow_log(path: Path) -> None:  # pragma: no cover - interactive, blocks on I/O
    """Best-effort ``tail -f``: print new lines as they are appended."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            while True:
                line = handle.readline()
                if line:
                    print(line.rstrip("\n"))
                    continue
                time.sleep(0.5)
    except KeyboardInterrupt:
        return
    except OSError:
        return


def cmd_prune(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.older_than is not None:
        try:
            age_seconds = parse_age_seconds(args.older_than)
        except ValueError as exc:
            raise SystemExit(str(exc))
    else:
        age_seconds = max(0, config.event_retention_days) * 24 * 60 * 60
        if age_seconds == 0:
            print("Event retention is set to forever; nothing pruned. Pass --older-than to override.")
            return
    cutoff = int(time.time()) - age_seconds
    size_before = db_size_bytes(config.database_path)
    conn = connect(config.database_path)
    try:
        init_db(conn)
        removed = prune_processed_events(conn, older_than_epoch=cutoff)
        vacuum_db(conn)
    finally:
        conn.close()
    size_after = db_size_bytes(config.database_path)
    reclaimed = max(0, size_before - size_after)
    print(f"Pruned {removed} processed event(s) older than {args.older_than or f'{config.event_retention_days}d'}.")
    print(f"Reclaimed {format_bytes(reclaimed)} (database now {format_bytes(size_after)}).")


def cmd_clear(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    clear_event_rows = args.events or args.clear_all
    clear_history = args.history or args.clear_all
    if not clear_event_rows and not clear_history:
        raise SystemExit("Nothing to clear. Pass --events, --history, or --all.")

    targets = []
    if clear_event_rows:
        targets.append("events")
    if clear_history:
        targets.append("notification + session history")
    if not _confirm_destructive(
        f"Clear {', '.join(targets)}?",
        assume_yes=args.yes,
        action=f"to clear {', '.join(targets)}",
    ):
        print("Aborted.")
        return

    conn = connect(config.database_path)
    try:
        init_db(conn)
        if clear_event_rows:
            removed = clear_events(conn)
            print(f"Cleared {removed} event(s).")
        if clear_history:
            notifications = clear_notifications(conn)
            sessions = clear_session_states(conn)
            reminders = clear_reminders(conn)
            print(
                f"Cleared {notifications} notification(s), {sessions} session state(s), "
                f"and {reminders} pending reminder(s)."
            )
    finally:
        conn.close()
    if clear_history:
        truncate_pipeline_log(config)
        print("Cleared the summary pipeline log.")


def _confirm_destructive(question: str, *, assume_yes: bool, action: str) -> bool:
    """Return whether an irreversible action may proceed.

    ``--yes`` always proceeds. An interactive TTY gets an arrow-key yes/no
    confirm defaulting to No. A non-interactive run (script, pipe, CI, cron)
    without ``--yes`` is REFUSED — the action is irreversible, so it must never
    happen unprompted; we print a hint and exit non-zero.
    """
    if assume_yes:
        return True
    if not _interactive():
        print(f"Refusing {action} without confirmation; pass --yes.")
        raise SystemExit(2)
    return confirm(question, default=False)


def cmd_autostart_enable(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if sys.platform != "darwin":
        print("! Autostart uses macOS launchd; not available on this platform.")
        return
    enabled = enable_autostart(config)
    set_autostart_managed(config.config_path, True)
    if enabled:
        print("Enabled autostart for: " + ", ".join(enabled))
    else:
        print("! Could not load any autostart agents (is launchctl available?).")
    for label in (DAEMON_LABEL, MENUBAR_LABEL):
        print(f"  {label}: {plist_path(label)}")


def cmd_autostart_disable(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if sys.platform != "darwin":
        print("! Autostart uses macOS launchd; not available on this platform.")
        return
    removed = disable_autostart(config)
    set_autostart_managed(config.config_path, False)
    if removed:
        print("Disabled autostart for: " + ", ".join(removed))
    else:
        print("Autostart was not installed.")


def cmd_autostart_status(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if sys.platform != "darwin":
        print("! Autostart uses macOS launchd; not available on this platform.")
        return
    status = autostart_status(config)
    print(f"Autostart managed: {'yes' if config.autostart_managed else 'no'}")
    for label in (DAEMON_LABEL, MENUBAR_LABEL):
        info = status.get(label, {})
        present = "present" if info.get("plist_present") else "absent"
        loaded = "loaded" if info.get("loaded") else "not loaded"
        print(f"  {label}: plist {present}, {loaded}")
        print(f"    {plist_path(label)}")


def cmd_uninstall(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.target is not None:
        _uninstall_target(args.target)
        return
    _uninstall_all(config, args)


def _uninstall_target(target: str) -> None:
    remover_name = _UNINSTALL_REMOVER_NAMES.get(target)
    if remover_name is None:  # pragma: no cover - argparse restricts choices
        raise SystemExit(f"Unknown integration '{target}'.")
    remover = globals()[remover_name]
    result = remover()
    if target == "pi":
        print(f"pi extension removed: {result.extension_removed}")
        print(f"  extension: {result.extension_path}")
    else:
        removed = ", ".join(result.removed_events) if result.removed_events else "none"
        path = getattr(result, "settings_path", None) or getattr(result, "hooks_path", None)
        print(f"Removed {target} hooks: {removed}")
        print(f"  file: {path}")
    print(f"  wrapper removed: {result.wrapper_removed}")
    print(f"Left ~/.voiccce in place. Run `voiccce uninstall` (no target) to tear down everything.")


def _uninstall_all(config: AgentVoiceConfig, args: argparse.Namespace) -> None:
    wired = detect_wired_integrations(config)
    summary = ", ".join(wired) if wired else "no wired integrations"
    purge_note = " and DELETE ~/.voiccce" if args.purge else " (keeping ~/.voiccce)"
    action = "to purge Voiccce" if args.purge else "to tear down Voiccce"
    if not _confirm_destructive(
        f"Tear down Voiccce ({summary}){purge_note}?",
        assume_yes=args.yes,
        action=action,
    ):
        print("Aborted.")
        return

    plan = TeardownPlan(purge_data=args.purge, restore_backups=args.restore_backups)
    report = run_teardown(config, plan)
    _print_teardown_report(report)


def _print_teardown_report(report: TeardownReport) -> None:
    if report.stopped:
        print("Stopped: " + ", ".join(report.stopped))
    for agent, events in report.removed_hooks.items():
        if isinstance(events, bool):
            print(f"Removed {agent} hook: {events}")
        else:
            print(f"Removed {agent} hooks: {', '.join(events) if events else 'none'}")
    if report.removed_wrappers:
        print("Removed wrappers: " + ", ".join(report.removed_wrappers))
    if report.removed_autostart:
        print("Removed autostart: " + ", ".join(report.removed_autostart))
    print(f"Keychain secret deleted: {report.keychain_deleted}")
    if report.backups_restored:
        print("Restored backups: " + ", ".join(report.backups_restored))
    print(f"Data directory removed: {report.data_removed}")
    for note in report.notes:
        print(f"  note: {note}")
    print("Finish removing the package with:")
    print("  " + " ".join(report.package_command))


if __name__ == "__main__":
    main()
