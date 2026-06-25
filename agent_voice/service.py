from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AgentVoiceConfig
from .heartbeat import heartbeat_age_seconds

# A daemon with no live pid file is still considered running when its heartbeat is
# fresher than this many seconds — covers launchd-managed daemons whose pid file
# may be absent/replaced. Mirrors doctor's heartbeat-stale floor so status and
# health agree.
HEARTBEAT_FRESH_POLL_MULTIPLIER = 5
HEARTBEAT_FRESH_MIN_SECONDS = 120


@dataclass(frozen=True, slots=True)
class ServicePaths:
    pid_path: Path
    log_path: Path


def service_paths(config: AgentVoiceConfig) -> ServicePaths:
    home = config.config_path.parent
    return ServicePaths(pid_path=home / "daemon.pid", log_path=home / "daemon.log")


def menubar_service_paths(config: AgentVoiceConfig) -> ServicePaths:
    home = config.config_path.parent
    return ServicePaths(pid_path=home / "menubar.pid", log_path=home / "menubar.log")


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid(pid_path: Path) -> int | None:
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def is_pid_stale(pid_path: Path) -> bool:
    """Return True when ``pid_path`` names a pid that is no longer running.

    A missing pid file or an unparseable one is *not* considered stale: there is
    no leftover pid to clean up. Only a recorded-but-dead pid is stale.
    """
    pid = read_pid(pid_path)
    if pid is None:
        return False
    return not is_pid_running(pid)


def stale_pid_warnings(config: AgentVoiceConfig) -> list[tuple[str, int]]:
    """Return ``(label, pid)`` pairs for known pid files whose process is dead.

    Lets callers surface "the daemon pid file points at a process that is gone"
    without itself deciding how to recover. Labels match the service names.
    """
    warnings: list[tuple[str, int]] = []
    for label, paths in (
        ("daemon", service_paths(config)),
        ("menubar", menubar_service_paths(config)),
    ):
        pid = read_pid(paths.pid_path)
        if pid is not None and not is_pid_running(pid):
            warnings.append((label, pid))
    return warnings


def rotate_log_if_needed(log_path: Path, max_bytes: int) -> None:
    """Rotate ``log_path`` to ``<name>.log.1`` once it grows past ``max_bytes``.

    A ``max_bytes`` of 0 (or negative) disables rotation. The previous rotated
    file is replaced. Missing log files and rotation errors are ignored so a log
    problem never blocks starting a service.
    """
    if max_bytes <= 0:
        return
    try:
        size = log_path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    rotated = log_path.with_suffix(".log.1")
    try:
        os.replace(log_path, rotated)
    except OSError:
        pass


def start_daemon(config: AgentVoiceConfig) -> int:
    return _start_background_process(
        config,
        paths=service_paths(config),
        command=["daemon"],
    )


def start_menubar(config: AgentVoiceConfig) -> int:
    return _start_background_process(
        config,
        paths=menubar_service_paths(config),
        command=["menubar"],
    )


def service_python_invocation(config: AgentVoiceConfig, command: list[str]) -> list[str]:
    """Return the canonical argv for running ``command`` against this config.

    This is the single source of truth shared by the :class:`subprocess.Popen`
    launcher and the launchd plist generator, so a background process started by
    hand and one started by autostart invoke the package identically.
    """
    return [
        sys.executable,
        "-m",
        "agent_voice",
        "--config",
        str(config.config_path),
        *command,
    ]


def _start_background_process(config: AgentVoiceConfig, *, paths: ServicePaths, command: list[str]) -> int:
    existing_pid = read_pid(paths.pid_path)
    if existing_pid and is_pid_running(existing_pid):
        return existing_pid

    paths.pid_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log_if_needed(paths.log_path, config.max_log_bytes)
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo_root}:{env.get('PYTHONPATH', '')}"
    log_file = paths.log_path.open("ab")
    process = subprocess.Popen(
        service_python_invocation(config, command),
        cwd=str(repo_root),
        env=env,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    paths.pid_path.write_text(str(process.pid), encoding="utf-8")
    time.sleep(0.2)
    if not is_pid_running(process.pid):
        raise RuntimeError(f"background process exited immediately; see {paths.log_path}")
    return process.pid


def stop_daemon(config: AgentVoiceConfig) -> int | None:
    return _stop_background_process(service_paths(config))


def stop_menubar(config: AgentVoiceConfig) -> int | None:
    return _stop_background_process(menubar_service_paths(config))


def _stop_background_process(paths: ServicePaths) -> int | None:
    pid = read_pid(paths.pid_path)
    if not pid:
        return None
    if is_pid_running(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not is_pid_running(pid):
                break
            time.sleep(0.1)
        # Escalate to SIGKILL if the process ignored SIGTERM, so a wedged service
        # cannot leave a live process behind once we remove its pid file.
        if is_pid_running(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            for _ in range(10):
                if not is_pid_running(pid):
                    break
                time.sleep(0.1)
    paths.pid_path.unlink(missing_ok=True)
    return pid


def _heartbeat_is_fresh(config: AgentVoiceConfig) -> bool:
    """Whether the daemon heartbeat is recent enough to imply a live daemon.

    Used as a fallback for launchd-managed daemons that may not leave a live pid
    file: a heartbeat within the freshness window means a daemon is ticking even
    though no pid is recorded here. The window scales with the poll interval and is
    floored so a tiny interval does not flap on a slow tick.
    """
    age = heartbeat_age_seconds(config)
    if age is None:
        return False
    threshold = max(
        HEARTBEAT_FRESH_MIN_SECONDS,
        HEARTBEAT_FRESH_POLL_MULTIPLIER * (config.poll_interval_ms / 1000.0),
    )
    return age <= threshold


def daemon_status(config: AgentVoiceConfig) -> tuple[int | None, bool]:
    paths = service_paths(config)
    pid = read_pid(paths.pid_path)
    if pid and is_pid_running(pid):
        return pid, True
    # No live pid file (e.g. a launchd-managed daemon that did not record one, or a
    # replaced file): treat a fresh heartbeat as proof the daemon is running so it
    # is not reported DOWN.
    if _heartbeat_is_fresh(config):
        return pid, True
    return pid, False


def menubar_status(config: AgentVoiceConfig) -> tuple[int | None, bool]:
    paths = menubar_service_paths(config)
    pid = read_pid(paths.pid_path)
    return pid, bool(pid and is_pid_running(pid))
