"""Health checks for a Voiccce install.

A single place that inspects the on-disk state — config, database, agent hook
wiring, hook-wrapper interpreter, OpenAI key, platform tools, daemon liveness,
mute state, and failed events — and reports each as a :class:`CheckResult`.
The CLI's ``voiccce doctor`` command renders these, and ``voiccce status`` reuses
:func:`inspect_agent_wiring` as the single source of truth for "which agents are
wired".

Every check is best-effort and never raises: an unexpected error is surfaced as a
failing (or, for informational checks, passing) :class:`CheckResult` rather than a
traceback, so one broken subsystem cannot mask the rest of the report.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import config as config_module
from . import db, heartbeat, secrets, service
from . import launchagent
from .config import AgentVoiceConfig
from .installer import WrapperImportError, verify_wrapper_imports
from .installer import claude_code, codex, pi


# Heartbeat is considered stale once it is older than this many poll intervals,
# floored at a sane minimum so a tiny poll interval does not flag a healthy
# daemon during a slow tick.
HEARTBEAT_STALE_POLL_MULTIPLIER = 5
HEARTBEAT_STALE_MIN_SECONDS = 120

# Platform tools the voice/desktop pipeline relies on. ``afplay``/``say`` drive
# playback, ``afinfo`` measures audio duration, and ``osascript`` shows desktop
# notifications.
REQUIRED_TOOLS: tuple[str, ...] = ("afplay", "say", "afinfo", "osascript")


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    hint: str = ""


@dataclass(frozen=True, slots=True)
class AgentWiring:
    agent: str
    wired: bool
    events: tuple[str, ...]
    detail: str


def inspect_agent_wiring(config: AgentVoiceConfig) -> list[AgentWiring]:
    """Report, per supported agent, whether a Voiccce hook is wired and which
    lifecycle events it covers.

    Reads the live integration files (Claude ``settings.json``, Codex
    ``hooks.json``, the pi extension) and detects Voiccce hooks by the same
    ``VOICCCE=1`` marker / wrapper reference the installers write. Missing or
    malformed files report ``wired=False`` with an explanatory detail. This is the
    single source of truth reused by ``voiccce status``.
    """
    return [
        _inspect_claude_wiring(),
        _inspect_codex_wiring(),
        _inspect_pi_wiring(),
    ]


def _inspect_claude_wiring() -> AgentWiring:
    path = claude_code.PERSONAL_SETTINGS_PATH
    settings = _read_json(path)
    if settings is None:
        return AgentWiring("claude-code", False, (), f"no settings at {path}")
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    events = _wired_events_from_hook_map(hooks, claude_code.ENTRY_MARKERS)
    return _wiring_from_events("claude-code", events, path)


def _inspect_codex_wiring() -> AgentWiring:
    path = (codex._default_codex_home() / "hooks.json").expanduser()
    config = _read_json(path)
    if config is None:
        return AgentWiring("codex", False, (), f"no hooks file at {path}")
    hooks = config.get("hooks") if isinstance(config, dict) else None
    events = _wired_events_from_hook_map(hooks, codex.ENTRY_MARKERS)
    return _wiring_from_events("codex", events, path)


def _inspect_pi_wiring() -> AgentWiring:
    agent_dir = pi._resolve_agent_dir(None).expanduser()
    path = agent_dir / "extensions" / "voiccce.ts"
    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return AgentWiring("pi", False, (), f"no extension at {path}")
    if pi.MARKER not in content:
        return AgentWiring("pi", False, (), f"extension at {path} is not a Voiccce hook")
    # The generated extension wires a fixed set of lifecycle events; report only
    # those whose pi handler the marker file actually registers.
    events = tuple(event for event in pi.PI_HOOKS if event in content)
    if not events:
        events = pi.PI_HOOKS
    return AgentWiring("pi", True, events, f"wired in {path}")


def _wiring_from_events(agent: str, events: tuple[str, ...], path: Path) -> AgentWiring:
    if events:
        return AgentWiring(agent, True, events, f"wired in {path}")
    return AgentWiring(agent, False, (), f"no Voiccce hook in {path}")


def _wired_events_from_hook_map(hooks: object, markers: tuple[str, ...]) -> tuple[str, ...]:
    """Return the hook/event names whose entries carry one of ``markers``.

    The Claude and Codex files share a shape: ``{"hooks": {EventName: [entry,
    ...]}}`` where each entry holds ``{"hooks": [{"command": "..."}]}``. An event
    is wired when any of its entries' commands contain a Voiccce marker.
    """
    if not isinstance(hooks, dict):
        return ()
    wired: list[str] = []
    for event_name, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        if any(_entry_has_marker(entry, markers) for entry in entries):
            wired.append(str(event_name))
    return tuple(wired)


def _entry_has_marker(entry: object, markers: tuple[str, ...]) -> bool:
    if not isinstance(entry, dict):
        return False
    inner = entry.get("hooks", [])
    if not isinstance(inner, list):
        return False
    for hook in inner:
        command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
        if any(marker in command for marker in markers):
            return True
    return False


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else {}


def check_config(config_path: Path | str | None) -> CheckResult:
    """Confirm the config file parses. A malformed file fails with its own hint."""
    try:
        config = config_module.load_config(config_path)
    except config_module.ConfigError as exc:
        return CheckResult(
            name="config",
            ok=False,
            detail=str(exc),
            hint=exc.hint,
        )
    return CheckResult(
        name="config",
        ok=True,
        detail=f"loaded {config.config_path}",
    )


def check_database(config: AgentVoiceConfig) -> CheckResult:
    """Confirm the events database opens and initializes cleanly."""
    try:
        conn = db.connect(config.database_path)
    except sqlite3.DatabaseError as exc:
        return CheckResult(
            name="database",
            ok=False,
            detail=f"could not open {config.database_path}: {exc}",
            hint="Delete or restore the database file, then re-run setup.",
        )
    try:
        db.init_db(conn)
    except sqlite3.DatabaseError as exc:
        return CheckResult(
            name="database",
            ok=False,
            detail=f"could not initialize {config.database_path}: {exc}",
            hint="The database may be corrupt; delete it and re-run setup.",
        )
    finally:
        conn.close()
    size = db.db_size_bytes(config.database_path)
    return CheckResult(
        name="database",
        ok=True,
        detail=f"{config.database_path} ({size} bytes)",
    )


def check_hooks_wired(config: AgentVoiceConfig) -> CheckResult:
    """Summarize :func:`inspect_agent_wiring`: at least one agent must be wired."""
    wirings = inspect_agent_wiring(config)
    wired = [w for w in wirings if w.wired]
    if not wired:
        return CheckResult(
            name="hooks",
            ok=False,
            detail="no agents wired",
            hint="Run `voiccce setup` (or `voiccce install <agent>`) to wire an agent.",
        )
    summary = ", ".join(f"{w.agent} ({len(w.events)})" for w in wired)
    return CheckResult(
        name="hooks",
        ok=True,
        detail=f"wired: {summary}",
    )


def check_wrapper_import(config: AgentVoiceConfig) -> CheckResult:
    """Confirm the installed hook wrapper's interpreter can import ``agent_voice``.

    Best-effort: when no wrapper is installed the check passes informationally;
    otherwise the wrapper script is parsed for its ``PYTHON_BIN``/``REPO_ROOT`` and
    fed to :func:`verify_wrapper_imports`.
    """
    wrapper_path = claude_code.WRAPPER_PATH
    parsed = _parse_wrapper(wrapper_path)
    if parsed is None:
        return CheckResult(
            name="wrapper",
            ok=True,
            detail="no hook wrapper installed (skipped)",
        )
    python_executable, repo_root = parsed
    try:
        verify_wrapper_imports(Path(python_executable), Path(repo_root))
    except WrapperImportError as exc:
        return CheckResult(
            name="wrapper",
            ok=False,
            detail=str(exc).splitlines()[0],
            hint="Re-run setup with the interpreter that has voiccce installed.",
        )
    return CheckResult(
        name="wrapper",
        ok=True,
        detail=f"{python_executable} can import agent_voice",
    )


def _parse_wrapper(wrapper_path: Path) -> tuple[str, str] | None:
    """Extract ``(python_executable, repo_root)`` from a generated hook wrapper.

    The wrapper is a bash script with ``REPO_ROOT=<shell-quoted>`` and
    ``PYTHON_BIN=<shell-quoted>`` assignment lines. Returns ``None`` if the file is
    missing, unreadable, or does not carry both assignments.
    """
    try:
        text = wrapper_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    repo_root: str | None = None
    python_executable: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("REPO_ROOT="):
            repo_root = _unquote_assignment(stripped[len("REPO_ROOT="):])
        elif stripped.startswith("PYTHON_BIN="):
            python_executable = _unquote_assignment(stripped[len("PYTHON_BIN="):])
    if repo_root is None or python_executable is None:
        return None
    return python_executable, repo_root


def _unquote_assignment(value: str) -> str:
    try:
        parts = shlex.split(value)
    except ValueError:
        return value
    return parts[0] if parts else value


def check_openai_key(config: AgentVoiceConfig, *, validate: bool = True) -> CheckResult:
    """Check the OpenAI key when the backend needs it.

    For ``openai_tts`` the key must be present and (when ``validate``) valid. For
    any other backend this is an informational pass.
    """
    if config.voice_backend != "openai_tts":
        return CheckResult(
            name="openai_key",
            ok=True,
            detail=f"not required for backend {config.voice_backend}",
        )
    status = secrets.get_openai_secret_status(config)
    if not status.available:
        return CheckResult(
            name="openai_key",
            ok=False,
            detail="no OpenAI API key found (env, .env, or keychain)",
            hint="Run `voiccce key set` or export the key env var.",
        )
    if not validate:
        return CheckResult(
            name="openai_key",
            ok=True,
            detail=f"present (source: {status.source}); validation skipped",
        )
    api_key, _ = secrets.resolve_openai_api_key(config)
    if not api_key:
        return CheckResult(
            name="openai_key",
            ok=False,
            detail="key reported present but could not be resolved",
            hint="Run `voiccce key set` to re-store the key.",
        )
    validation = secrets.validate_openai_tts_key(config, api_key)
    if not validation.ok:
        return CheckResult(
            name="openai_key",
            ok=False,
            detail=f"key from {status.source} failed validation: {validation.error}",
            hint="Replace the key with `voiccce key set`.",
        )
    return CheckResult(
        name="openai_key",
        ok=True,
        detail=f"present and valid (source: {status.source})",
    )


def check_required_tools() -> CheckResult:
    """Confirm the macOS audio/notification CLIs are on ``PATH``."""
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        return CheckResult(
            name="tools",
            ok=False,
            detail=f"missing: {', '.join(missing)}",
            hint="These ship with macOS; ensure /usr/bin is on PATH (voiccce targets macOS).",
        )
    return CheckResult(
        name="tools",
        ok=True,
        detail=f"found: {', '.join(REQUIRED_TOOLS)}",
    )


def check_daemon_health(config: AgentVoiceConfig) -> CheckResult:
    """Confirm the daemon process is alive and its heartbeat is recent.

    A dead-but-recorded pid is flagged via :func:`service.stale_pid_warnings`. When
    autostart is managed, the launchd status is appended to the detail. The
    heartbeat-stale threshold scales with the poll interval (see
    :data:`HEARTBEAT_STALE_POLL_MULTIPLIER`).
    """
    stale = service.stale_pid_warnings(config)
    pid, running = service.daemon_status(config)

    if not running:
        if any(label == "daemon" for label, _ in stale):
            stale_pid = next(p for label, p in stale if label == "daemon")
            return CheckResult(
                name="daemon",
                ok=False,
                detail=f"stale pid {stale_pid}: process is gone",
                hint="Run `voiccce restart` to clear the stale pid and start fresh.",
            )
        return CheckResult(
            name="daemon",
            ok=False,
            detail="daemon not running",
            hint="Run `voiccce start` (or `voiccce restart`).",
        )

    age = heartbeat.heartbeat_age_seconds(config)
    threshold = max(
        HEARTBEAT_STALE_MIN_SECONDS,
        HEARTBEAT_STALE_POLL_MULTIPLIER * (config.poll_interval_ms / 1000.0),
    )
    autostart_note = _autostart_note(config)
    if age is None:
        return CheckResult(
            name="daemon",
            ok=False,
            detail=f"running (pid {pid}) but no heartbeat recorded{autostart_note}",
            hint="Restart the daemon with `voiccce restart`.",
        )
    if age > threshold:
        return CheckResult(
            name="daemon",
            ok=False,
            detail=f"running (pid {pid}) but heartbeat is {age:.0f}s old (> {threshold:.0f}s){autostart_note}",
            hint="The daemon may be wedged; run `voiccce restart`.",
        )
    return CheckResult(
        name="daemon",
        ok=True,
        detail=f"running (pid {pid}), heartbeat {age:.0f}s ago{autostart_note}",
    )


def _autostart_note(config: AgentVoiceConfig) -> str:
    if not config.autostart_managed:
        return ""
    try:
        status = launchagent.autostart_status(config)
    except Exception:  # pragma: no cover - launchctl is environment dependent
        return "; autostart managed"
    daemon = status.get(launchagent.DAEMON_LABEL, {})
    loaded = "loaded" if daemon.get("loaded") else "not loaded"
    return f"; autostart managed (launchd: {loaded})"


def check_mute(config: AgentVoiceConfig) -> CheckResult:
    """Report the voice mute state (informational pass either way)."""
    try:
        from .runtime import voice_mute_status
    except Exception:  # pragma: no cover - runtime should always import
        return CheckResult(name="mute", ok=True, detail="mute state unavailable")
    status = voice_mute_status(config)
    if status.muted:
        detail = "voice is muted"
        if status.muted_until:
            detail += f" until epoch {status.muted_until}"
        return CheckResult(
            name="mute",
            ok=True,
            detail=detail,
            hint="Run `voiccce unmute` to resume voice notifications.",
        )
    return CheckResult(name="mute", ok=True, detail="voice not muted")


def check_failed_events(config: AgentVoiceConfig) -> CheckResult:
    """Count events stuck in the ``failed`` status."""
    try:
        conn = db.connect(config.database_path)
    except sqlite3.DatabaseError as exc:
        return CheckResult(
            name="failed_events",
            ok=False,
            detail=f"could not open database: {exc}",
            hint="Run `voiccce doctor` after fixing the database.",
        )
    try:
        db.init_db(conn)
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE status = 'failed'"
        ).fetchone()
        failed = int(row[0]) if row else 0
    except sqlite3.DatabaseError as exc:
        return CheckResult(
            name="failed_events",
            ok=False,
            detail=f"could not query events: {exc}",
            hint="The database may be corrupt; re-run setup.",
        )
    finally:
        conn.close()
    if failed:
        return CheckResult(
            name="failed_events",
            ok=False,
            detail=f"{failed} failed event(s)",
            hint="Run `voiccce events` to inspect the failures.",
        )
    return CheckResult(
        name="failed_events",
        ok=True,
        detail="no failed events",
    )


def run_doctor(config: AgentVoiceConfig, *, validate_key: bool = True) -> list[CheckResult]:
    """Run every health check and return the results in a stable order.

    ``validate_key=False`` skips the live OpenAI key validation (still checking the
    key is present) so the report is usable offline and in CI.
    """
    return [
        check_config(config.config_path),
        check_database(config),
        check_hooks_wired(config),
        check_wrapper_import(config),
        check_openai_key(config, validate=validate_key),
        check_required_tools(),
        check_daemon_health(config),
        check_mute(config),
        check_failed_events(config),
    ]


def doctor_ok(results: list[CheckResult]) -> bool:
    """True iff no :class:`CheckResult` failed."""
    return all(result.ok for result in results)
