from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_voice.config import DEFAULT_CONFIG_PATH, write_default_config
from agent_voice.db import connect, init_db
from agent_voice.config import load_config
from agent_voice.installer import verify_wrapper_imports


PERSONAL_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
VOICCCE_HOME = Path.home() / ".voiccce"
WRAPPER_PATH = VOICCCE_HOME / "bin" / "voiccce-claude-hook"
MARKER = "VOICCCE=1"
LEGACY_MARKERS = ("AGENT_CHIME=1",)
ENTRY_MARKERS = (MARKER, *LEGACY_MARKERS)

CLAUDE_HOOKS = {
    "Stop": {"matcher": None},
    "Notification": {"matcher": "*"},
    "PermissionRequest": {"matcher": "*"},
    "StopFailure": {"matcher": None},
    "SubagentStop": {"matcher": None},
    "UserPromptSubmit": {"matcher": None},
}


@dataclass(frozen=True, slots=True)
class ClaudeInstallResult:
    settings_path: Path
    backup_path: Path
    wrapper_path: Path
    config_path: Path
    database_path: Path
    installed_events: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaudeRemoveResult:
    settings_path: Path
    backup_path: Path | None
    removed_events: tuple[str, ...]
    wrapper_path: Path
    wrapper_removed: bool


def install_claude_code_personal(
    *,
    repo_root: Path | None = None,
    settings_path: Path = PERSONAL_SETTINGS_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    wrapper_path: Path = WRAPPER_PATH,
    python_executable: str | Path | None = None,
    verify: bool = False,
) -> ClaudeInstallResult:
    repo_root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
    config_path = config_path.expanduser().resolve()
    settings_path = settings_path.expanduser().resolve()
    wrapper_path = wrapper_path.expanduser().resolve()
    python_executable = Path(python_executable or sys.executable).expanduser().resolve()

    write_default_config(config_path)
    config = load_config(config_path)
    conn = connect(config.database_path)
    try:
        init_db(conn)
    finally:
        conn.close()

    _write_wrapper(wrapper_path, repo_root, config_path, python_executable)
    if verify:
        verify_wrapper_imports(python_executable, repo_root)
    settings = _read_settings(settings_path)
    backup_path = _backup_settings(settings_path)
    settings.setdefault("hooks", {})

    for hook_name, hook_config in CLAUDE_HOOKS.items():
        command = f"{MARKER} {wrapper_path} {hook_name}"
        entry: dict[str, object] = {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                }
            ]
        }
        if hook_config["matcher"] is not None:
            entry["matcher"] = hook_config["matcher"]

        existing_entries = settings["hooks"].setdefault(hook_name, [])
        settings["hooks"][hook_name] = _without_voiccce_entries(existing_entries)
        settings["hooks"][hook_name].append(entry)

    _write_settings(settings_path, settings)
    return ClaudeInstallResult(
        settings_path=settings_path,
        backup_path=backup_path,
        wrapper_path=wrapper_path,
        config_path=config_path,
        database_path=config.database_path,
        installed_events=tuple(CLAUDE_HOOKS.keys()),
    )


def remove_claude_code_personal(
    *,
    settings_path: Path = PERSONAL_SETTINGS_PATH,
    wrapper_path: Path = WRAPPER_PATH,
) -> ClaudeRemoveResult:
    """Strip Voiccce hook entries from the Claude settings, idempotently.

    Marker-tagged entries are removed via :func:`_without_voiccce_entries`
    while every other hook the user configured is preserved. The settings file
    is backed up before it is rewritten, and the generated hook wrapper is
    deleted. When nothing is installed this is a safe no-op: no backup is taken,
    ``removed_events`` is empty, and the file is left untouched.
    """
    settings_path = settings_path.expanduser().resolve()
    wrapper_path = wrapper_path.expanduser().resolve()

    settings = _read_settings(settings_path)
    hooks = settings.get("hooks")

    removed_events: list[str] = []
    if isinstance(hooks, dict):
        for hook_name, entries in list(hooks.items()):
            if not isinstance(entries, list):
                continue
            kept = _without_voiccce_entries(entries)
            if len(kept) == len(entries):
                continue
            removed_events.append(hook_name)
            if kept:
                hooks[hook_name] = kept
            else:
                del hooks[hook_name]

    backup_path: Path | None = None
    if removed_events:
        backup_path = _backup_settings(settings_path)
        _write_settings(settings_path, settings)

    wrapper_removed = _remove_wrapper(wrapper_path)
    return ClaudeRemoveResult(
        settings_path=settings_path,
        backup_path=backup_path,
        removed_events=tuple(removed_events),
        wrapper_path=wrapper_path,
        wrapper_removed=wrapper_removed,
    )


def restore_latest_backup(settings_path: Path = PERSONAL_SETTINGS_PATH) -> Path | None:
    """Restore the most recent Voiccce backup over ``settings_path``.

    Returns the backup that was restored, or ``None`` when no backup exists.
    """
    settings_path = settings_path.expanduser().resolve()
    return _restore_backup(settings_path, _latest_backup(settings_path))


def restore_original_backup(settings_path: Path = PERSONAL_SETTINGS_PATH) -> Path | None:
    """Restore the OLDEST Voiccce backup over ``settings_path``.

    The oldest backup is the one taken just before the very first install, so it
    captures the user's pre-install settings. Restoring it during teardown undoes
    Voiccce cleanly, whereas restoring the *latest* backup would re-apply the
    still-installed state captured by :func:`remove_claude_code_personal`.

    Returns the backup that was restored, or ``None`` when no backup exists.
    """
    settings_path = settings_path.expanduser().resolve()
    return _restore_backup(settings_path, _oldest_backup(settings_path))


def _restore_backup(settings_path: Path, backup_path: Path | None) -> Path | None:
    if backup_path is None:
        return None
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
    settings_path.chmod(backup_path.stat().st_mode & 0o777)
    return backup_path


def _sorted_backups(settings_path: Path) -> list[Path]:
    prefix = f"{settings_path.name}.voiccce-backup."
    return sorted(
        (p for p in settings_path.parent.glob(f"{settings_path.name}.voiccce-backup.*") if p.name.startswith(prefix)),
        key=lambda p: p.name,
    )


def _latest_backup(settings_path: Path) -> Path | None:
    candidates = _sorted_backups(settings_path)
    return candidates[-1] if candidates else None


def _oldest_backup(settings_path: Path) -> Path | None:
    candidates = _sorted_backups(settings_path)
    return candidates[0] if candidates else None


def _remove_wrapper(wrapper_path: Path) -> bool:
    if not wrapper_path.exists():
        return False
    try:
        wrapper_path.unlink()
    except OSError:
        return False
    return True


def _read_settings(settings_path: Path) -> dict[str, object]:
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        return {}
    return json.loads(settings_path.read_text(encoding="utf-8"))


def _unique_backup_path(settings_path: Path) -> Path:
    """Return a non-existent backup path for ``settings_path``.

    The base stamp has microsecond resolution so two backups taken in the same
    second do not collide; a counter suffix is appended as a last-resort guard
    when even the microsecond stamp matches (or the file already exists), so a
    fast install+remove never overwrites the pre-install backup.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    base = settings_path.with_name(f"{settings_path.name}.voiccce-backup.{stamp}")
    if not base.exists():
        return base
    for counter in range(1, 1000):
        candidate = base.with_name(f"{base.name}.{counter}")
        if not candidate.exists():
            return candidate
    return base.with_name(f"{base.name}.{os.getpid()}")


def _backup_settings(settings_path: Path) -> Path:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = _unique_backup_path(settings_path)
    if settings_path.exists():
        backup_path.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")
        backup_path.chmod(settings_path.stat().st_mode & 0o777)
    else:
        backup_path.write_text("{}\n", encoding="utf-8")
        backup_path.chmod(0o600)
    return backup_path


def _write_settings(settings_path: Path, settings: dict[str, object]) -> None:
    tmp_path = settings_path.with_suffix(settings_path.suffix + ".voiccce-tmp")
    tmp_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    existing_mode = settings_path.stat().st_mode & 0o777 if settings_path.exists() else 0o600
    tmp_path.chmod(existing_mode)
    os.replace(tmp_path, settings_path)


def _without_voiccce_entries(entries: object) -> list[object]:
    if not isinstance(entries, list):
        return []
    kept = []
    for entry in entries:
        if _entry_contains_marker(entry):
            continue
        kept.append(entry)
    return kept


def _entry_contains_marker(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks", [])
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
        if any(marker in command for marker in ENTRY_MARKERS):
            return True
    return False


def _write_wrapper(
    wrapper_path: Path,
    repo_root: Path,
    config_path: Path,
    python_executable: Path,
) -> None:
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = VOICCCE_HOME / "hook.log"
    repo_root_value = shlex.quote(str(repo_root))
    config_path_value = shlex.quote(str(config_path))
    log_path_value = shlex.quote(str(log_path))
    python_executable_value = shlex.quote(str(python_executable))
    content = f"""#!/usr/bin/env bash
set -u

HOOK_NAME="${{1:-Stop}}"
REPO_ROOT={repo_root_value}
CONFIG_PATH={config_path_value}
LOG_PATH={log_path_value}
PYTHON_BIN={python_executable_value}

mkdir -p "$(dirname "$LOG_PATH")"
cd "$REPO_ROOT" || exit 0
PYTHONPATH="$REPO_ROOT:${{PYTHONPATH:-}}" "$PYTHON_BIN" -m agent_voice --config "$CONFIG_PATH" collect claude-code --hook "$HOOK_NAME" >> "$LOG_PATH" 2>&1 || true
exit 0
"""
    wrapper_path.write_text(content, encoding="utf-8")
    wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
