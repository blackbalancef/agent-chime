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
from pathlib import Path
from typing import TypeVar

from .config import (
    AgentVoiceConfig,
    load_config,
    normalize_language,
    set_config_language,
    set_voice_config,
    write_default_config,
)
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
from .secrets import delete_openai_keychain_secret, get_openai_secret_status, set_openai_keychain_secret
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
    install.add_argument("--pi-home", help="pi home directory (default ~/.pi). Installs a global extension.")
    install.set_defaults(handler=cmd_install)

    setup = subparsers.add_parser(
        "setup",
        help="One command to set up everything: OpenAI key, voice, hooks, daemon, and a test",
    )
    setup.add_argument(
        "target",
        nargs="?",
        choices=["claude-code", "codex", "pi", "both"],
        help="Which agent(s) to wire hooks for (prompted if omitted)",
    )
    setup.add_argument("--voice", default=None, help="Voice name (default: marin for OpenAI TTS, Alex for --local)")
    setup.add_argument("--local", action="store_true", help="Use the local macOS say voice instead of OpenAI TTS (no API key)")
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
    setup.add_argument("--pi-home", help="pi home directory (default ~/.pi). Installs a global extension.")
    setup.set_defaults(handler=cmd_setup)

    start = subparsers.add_parser("start", help="Start daemon in the background")
    start.set_defaults(handler=cmd_start)

    stop = subparsers.add_parser("stop", help="Stop background daemon")
    stop.set_defaults(handler=cmd_stop)

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
    config_cmd.add_argument("--language", choices=["en", "ru"], help="Notification language")
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

    if args.local:
        voice = args.voice or "Alex"
        set_voice_config(config_path, backend="macos_say", voice=voice)
        print(f"✓ Voice backend: macos_say (voice: {voice}, local macOS voice, no API key)")
    else:
        voice = args.voice or "marin"
        _ensure_openai_key(config, reset=args.reset_key)
        set_voice_config(config_path, backend="openai_tts", voice=voice)
        print(f"✓ Voice backend: openai_tts (voice: {voice})")

    target = _resolve_setup_target(args.target)
    installed: list[str] = []
    if target in {"claude-code", "both"}:
        result = _setup_install(
            "Claude settings.json",
            install_claude_code_personal,
            config_path=config_path,
            verify=True,
            **_claude_install_kwargs(args),
        )
        print(f"✓ Claude Code hooks → {result.settings_path}")
        installed.append("claude-code")
    if target in {"codex", "both"}:
        result = _setup_install(
            "Codex hooks.json",
            install_codex_personal,
            config_path=config_path,
            verify=True,
            **_codex_install_kwargs(args),
        )
        print(f"✓ Codex hooks → {result.hooks_path}")
        installed.append("codex")
    if target == "pi":
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

    if not args.no_test:
        results = DeliveryRouter(config).deliver("Voiccce is ready.")
        if any(result.spoken for result in results):
            print("✓ Test sent — you should hear it now.")
        else:
            error = next((result.error for result in results if result.error), None)
            detail = f" ({error})" if error else ""
            print(f"! Test could not play audio{detail}. Check `voiccce status` and your OpenAI key.")

    _maybe_setup_menubar(config, choice=args.menubar)

    print(f"\nDone. Edit {config.config_path} to customize voice, messages, and summaries.")
    if "claude-code" in installed:
        print("Claude Code: if a session was already open, start a new one so it loads the hooks.")
    if "codex" in installed:
        print("Codex: open /hooks and trust the Voiccce hooks; restart codex app-server if it was running.")
    if "pi" in installed:
        print("pi: restart pi (or run /reload) so it loads the new ~/.pi extension.")


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


def _ensure_openai_key(config: AgentVoiceConfig, *, reset: bool) -> None:
    status = get_openai_secret_status(config)
    if status.available and not reset:
        print(f"✓ Using existing OpenAI key (from {status.source})")
        return
    key = getpass.getpass("OpenAI API key: ").strip()
    if not key:
        raise SystemExit("No key entered. Re-run `voiccce setup`, or use `--local` for the macOS voice.")
    try:
        set_openai_keychain_secret(config, key)
    except RuntimeError as exc:
        raise SystemExit(
            f"Could not save the key to macOS Keychain: {exc}. "
            "Put it in ~/.voiccce/.env as OPENAI_API_KEY=... instead."
        )
    print("✓ OpenAI key saved to macOS Keychain")


def _resolve_setup_target(target: str | None) -> str:
    if target:
        return target
    if sys.stdin.isatty():
        answer = input("Wire hooks for? [claude-code/codex/pi/both] (both): ").strip().lower()
        target = answer or "both"
    else:
        target = "both"
    if target not in {"claude-code", "codex", "pi", "both"}:
        raise SystemExit(f"Unknown target '{target}'. Choose claude-code, codex, pi, or both.")
    return target


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
    if sys.platform != "darwin":
        if choice:
            print("! Menu bar app is macOS-only; skipping.")
        return
    enable = choice
    if enable is None:
        if not sys.stdin.isatty():
            return
        answer = input("Enable the macOS menu bar app? [Y/n]: ").strip().lower()
        enable = answer in {"", "y", "yes"}
    if not enable:
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
    print(f"Language: {config.language}")
    print(f"Voice: {config.voice_backend} / {config.voice_name or '-'}")
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
        config_path = set_config_language(args.config, args.language)
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

    config = load_config(args.config)
    normalize_language(config.language)
    print(f"Config: {config.config_path}")
    print(f"Language: {config.language}")
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
