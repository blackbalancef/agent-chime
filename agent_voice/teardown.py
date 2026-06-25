"""Orchestrate a full, safe uninstall of Voiccce.

This module is the single entry point for tearing everything down: stopping the
background services, removing the macOS autostart agents, stripping the agent
hook integrations (Claude Code, Codex, pi), deleting the OpenAI keychain secret,
and — only when explicitly asked — purging the ``~/.voiccce`` home tree.

Every step is best-effort and idempotent: running it twice, or running it when
nothing is installed, never raises. Each piece is delegated to the module that
owns it (``service``, ``launchagent``, ``installer.*``, ``secrets``) so this file
stays a thin coordinator.

Note: this module intentionally does NOT import :mod:`agent_voice.cli`. The CLI
imports teardown to power its ``uninstall`` command, so importing it back would
create a cycle. The small pipx-vs-pip heuristic in
:func:`package_removal_command` is therefore duplicated locally from the
equivalent logic in ``cli`` (and ``menubar``) rather than shared.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import launchagent, secrets, service
from .config import DEFAULT_HOME, AgentVoiceConfig
from .installer import claude_code, codex, pi, remove_orphaned_wrappers


PACKAGE_NAME = "voiccce"


@dataclass(frozen=True, slots=True)
class TeardownPlan:
    """Which teardown steps to perform.

    ``targets`` lists the agent integrations to unwire. ``purge_data`` removes the
    whole ``~/.voiccce`` home (config, database, logs, pid files); when false the
    home is preserved and a note explains it was kept. ``restore_backups`` asks
    each integration to additionally restore its most recent pre-install backup
    on top of stripping our entries. ``remove_autostart`` and ``stop_services``
    gate the launchd and background-process cleanup respectively.
    """

    targets: tuple[str, ...] = ("claude-code", "codex", "pi")
    purge_data: bool = False
    restore_backups: bool = False
    remove_autostart: bool = True
    stop_services: bool = True


@dataclass(slots=True)
class TeardownReport:
    """What :func:`run_teardown` actually did, for the caller to render."""

    stopped: list[str] = field(default_factory=list)
    removed_hooks: dict[str, object] = field(default_factory=dict)
    removed_wrappers: list[str] = field(default_factory=list)
    removed_autostart: list[str] = field(default_factory=list)
    keychain_deleted: bool = False
    data_removed: bool = False
    backups_restored: list[str] = field(default_factory=list)
    package_command: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def detect_wired_integrations(
    config: AgentVoiceConfig,
    *,
    claude_settings_path: Path | None = None,
    codex_hooks_path: Path | None = None,
    pi_extension_path: Path | None = None,
) -> list[str]:
    """Return which of ``claude-code``/``codex``/``pi`` currently carry a Voiccce hook.

    Detection reuses each installer's own marker/path logic so it stays in sync
    with what install/remove write. The optional ``*_path`` overrides exist so
    tests (and unusual layouts) can point at throwaway files instead of the real
    ``~/.claude``/``~/.codex``/``~/.pi`` locations.
    """
    wired: list[str] = []
    if _claude_is_wired(claude_settings_path):
        wired.append("claude-code")
    if _codex_is_wired(codex_hooks_path):
        wired.append("codex")
    if _pi_is_wired(pi_extension_path):
        wired.append("pi")
    return wired


def package_removal_command() -> list[str]:
    """Return the command that uninstalls the ``voiccce`` package.

    Mirrors the pipx-venv heuristic used elsewhere: when the running interpreter
    lives in ``.../pipx/venvs/<name>`` and ``pipx`` is on PATH, prefer
    ``pipx uninstall <name>``; otherwise fall back to ``pip uninstall -y`` with
    the current interpreter. Duplicated here on purpose to avoid importing
    :mod:`agent_voice.cli` (which imports this module).
    """
    prefix = Path(sys.prefix)
    is_pipx_venv = prefix.parent.name == "venvs" and prefix.parent.parent.name == "pipx"
    if is_pipx_venv and shutil.which("pipx"):
        return ["pipx", "uninstall", prefix.name]
    return [sys.executable, "-m", "pip", "uninstall", "-y", PACKAGE_NAME]


def run_teardown(config: AgentVoiceConfig, plan: TeardownPlan) -> TeardownReport:
    """Execute ``plan`` against ``config`` and return a :class:`TeardownReport`.

    The steps run in dependency order — stop the running processes first, then
    unload autostart, then unwire the agents and remove leftover wrappers, then
    forget the secret, and finally (only when ``plan.purge_data``) delete the
    home. Each step is guarded so an already-removed or failing piece is recorded
    in ``notes`` rather than aborting the rest of the teardown.
    """
    report = TeardownReport()

    if plan.stop_services:
        _stop_services(config, report)

    if plan.remove_autostart:
        _remove_autostart(config, report)

    for target in plan.targets:
        _teardown_target(target, plan, report)

    _remove_wrappers(config, report)
    _delete_keychain_secret(config, report)
    _handle_data(config, plan, report)

    report.package_command = package_removal_command()
    return report


# --- step helpers ---------------------------------------------------------


def _stop_services(config: AgentVoiceConfig, report: TeardownReport) -> None:
    for label, stop in (("daemon", service.stop_daemon), ("menubar", service.stop_menubar)):
        try:
            pid = stop(config)
        except Exception as exc:  # never let one stuck service abort teardown
            report.notes.append(f"Could not stop {label}: {exc}")
            continue
        if pid is not None:
            report.stopped.append(label)


def _remove_autostart(config: AgentVoiceConfig, report: TeardownReport) -> None:
    try:
        status = launchagent.autostart_status(config)
    except Exception:
        status = {}
    present = any(
        bool(info.get("plist_present")) or bool(info.get("loaded"))
        for info in status.values()
    )
    # Always attempt disable when no status is available (best-effort), but skip
    # the work when we positively know nothing is installed.
    if status and not present:
        report.notes.append("Autostart not installed; nothing to disable.")
        return
    try:
        report.removed_autostart = launchagent.disable_autostart(config)
    except Exception as exc:
        report.notes.append(f"Could not disable autostart: {exc}")


def _teardown_target(target: str, plan: TeardownPlan, report: TeardownReport) -> None:
    handler = _TARGET_HANDLERS.get(target)
    if handler is None:
        report.notes.append(f"Unknown teardown target '{target}'; skipped.")
        return
    try:
        handler(plan, report)
    except Exception as exc:
        report.notes.append(f"Could not unwire {target}: {exc}")


def _teardown_claude(plan: TeardownPlan, report: TeardownReport) -> None:
    result = claude_code.remove_claude_code_personal()
    report.removed_hooks["claude-code"] = list(result.removed_events)
    if plan.restore_backups:
        # Restore the OLDEST backup (the pre-install snapshot), not the newest:
        # ``remove_claude_code_personal`` above just took a fresh backup of the
        # still-installed state, so restoring the latest would re-apply Voiccce.
        restored = claude_code.restore_original_backup()
        if restored is not None:
            report.backups_restored.append(f"claude-code:{restored}")


def _teardown_codex(plan: TeardownPlan, report: TeardownReport) -> None:
    result = codex.remove_codex_personal()
    report.removed_hooks["codex"] = list(result.removed_events)
    if plan.restore_backups:
        # Restore the OLDEST backup (the pre-install snapshot), not the newest:
        # ``remove_codex_personal`` above just took a fresh backup of the
        # still-installed state, so restoring the latest would re-apply Voiccce.
        restored = codex.restore_original_backup()
        if restored is not None:
            report.backups_restored.append(f"codex:{restored}")


def _teardown_pi(plan: TeardownPlan, report: TeardownReport) -> None:
    # pi has no settings file to back up — install writes a standalone generated
    # extension — so ``restore_backups`` is a no-op here beyond the removal.
    result = pi.remove_pi_personal()
    report.removed_hooks["pi"] = bool(result.extension_removed)


_TARGET_HANDLERS = {
    "claude-code": _teardown_claude,
    "codex": _teardown_codex,
    "pi": _teardown_pi,
}


def _remove_wrappers(config: AgentVoiceConfig, report: TeardownReport) -> None:
    home = config.config_path.parent
    try:
        removed = remove_orphaned_wrappers(home)
    except Exception as exc:
        report.notes.append(f"Could not remove leftover wrappers: {exc}")
        return
    report.removed_wrappers = [str(path) for path in removed]


def _delete_keychain_secret(config: AgentVoiceConfig, report: TeardownReport) -> None:
    try:
        report.keychain_deleted = bool(secrets.delete_openai_keychain_secret(config))
    except Exception as exc:
        report.notes.append(f"Could not delete keychain secret: {exc}")


def _handle_data(config: AgentVoiceConfig, plan: TeardownPlan, report: TeardownReport) -> None:
    home = config.config_path.parent
    if not plan.purge_data:
        report.notes.append(f"Kept data directory {home} (use purge to remove it).")
        return
    if not _is_voiccce_home(home):
        report.notes.append(
            f"Refusing to purge {home}: it does not look like a Voiccce home directory."
        )
        return
    try:
        shutil.rmtree(home, ignore_errors=True)
    except Exception as exc:  # pragma: no cover - rmtree swallows with ignore_errors
        report.notes.append(f"Could not remove data directory {home}: {exc}")
        return
    report.data_removed = not home.exists()
    if not report.data_removed:
        report.notes.append(f"Data directory {home} could not be fully removed.")


# --- detection helpers ----------------------------------------------------


def _claude_is_wired(settings_path: Path | None) -> bool:
    path = (settings_path or claude_code.PERSONAL_SETTINGS_PATH).expanduser()
    settings = _read_json(path)
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    return _hooks_mapping_has_marker(hooks, claude_code._entry_contains_marker)


def _codex_is_wired(hooks_path: Path | None) -> bool:
    path = (hooks_path or codex._default_codex_home() / "hooks.json").expanduser()
    config = _read_json(path)
    hooks = config.get("hooks") if isinstance(config, dict) else None
    return _hooks_mapping_has_marker(hooks, codex._entry_contains_marker)


def _pi_is_wired(extension_path: Path | None) -> bool:
    if extension_path is not None:
        path = extension_path.expanduser()
    else:
        agent_dir = pi._resolve_agent_dir(None)
        path = (agent_dir / "extensions" / "voiccce.ts").expanduser()
    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    return pi.MARKER in content


def _hooks_mapping_has_marker(hooks: object, entry_has_marker) -> bool:
    if not isinstance(hooks, dict):
        return False
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        if any(entry_has_marker(entry) for entry in entries):
            return True
    return False


def _read_json(path: Path) -> object:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}


VOICCCE_CONFIG_MARKERS = ('api_key_keychain_service = "voiccce"', 'service = "voiccce"')


def _is_voiccce_home(home: Path) -> bool:
    """Guard against ``shutil.rmtree`` on an unexpected directory.

    Only directories that are unmistakably a Voiccce home are accepted:

    * named exactly ``.voiccce``; or
    * the configured default home (``config.DEFAULT_HOME``); or
    * holding BOTH telltale files we create together
      (``config.toml`` *and* ``events.sqlite3``); or
    * holding a ``config.toml`` that carries a Voiccce authorship marker.

    A bare ``config.toml`` on its own is *not* enough — it is used by many tools
    (black/ruff/...), so ``voiccce --config ~/proj/config.toml uninstall --purge``
    must never be able to ``rmtree`` an unrelated project directory. The
    filesystem root and the user's home are never accepted.
    """
    home = home.expanduser()
    try:
        home = home.resolve()
    except OSError:
        return False
    if home in (Path(home.anchor), Path.home().resolve()):
        return False
    if home.name == ".voiccce":
        return True
    try:
        if home == DEFAULT_HOME.expanduser().resolve():
            return True
    except OSError:
        pass
    config_file = home / "config.toml"
    if config_file.exists() and (home / "events.sqlite3").exists():
        return True
    return _config_has_voiccce_marker(config_file)


def _config_has_voiccce_marker(config_file: Path) -> bool:
    try:
        text = config_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return False
    return any(marker in text for marker in VOICCCE_CONFIG_MARKERS)
