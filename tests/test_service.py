import gc
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

from agent_voice.config import AgentVoiceConfig
from agent_voice import service
from agent_voice.service import (
    daemon_status,
    is_pid_stale,
    menubar_service_paths,
    read_pid,
    rotate_log_if_needed,
    service_paths,
    service_python_invocation,
    stale_pid_warnings,
    stop_daemon,
)


def _config(tmp: str) -> AgentVoiceConfig:
    return AgentVoiceConfig(
        config_path=Path(tmp) / "config.toml",
        database_path=Path(tmp) / "events.sqlite3",
    )


class ServicePythonInvocationTests(unittest.TestCase):
    def test_invocation_is_canonical_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            argv = service_python_invocation(config, ["daemon"])
            self.assertEqual(
                argv,
                [
                    sys.executable,
                    "-m",
                    "agent_voice",
                    "--config",
                    str(config.config_path),
                    "daemon",
                ],
            )

    def test_invocation_appends_full_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            argv = service_python_invocation(config, ["menubar", "--once"])
            self.assertEqual(argv[-2:], ["menubar", "--once"])
            self.assertEqual(argv[3:5], ["--config", str(config.config_path)])


class RotateLogTests(unittest.TestCase):
    def test_rotates_when_over_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "daemon.log"
            log_path.write_bytes(b"x" * 100)

            rotate_log_if_needed(log_path, 50)

            rotated = log_path.with_suffix(".log.1")
            self.assertTrue(rotated.exists())
            self.assertFalse(log_path.exists())
            self.assertEqual(rotated.read_bytes(), b"x" * 100)

    def test_no_rotation_under_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "daemon.log"
            log_path.write_bytes(b"x" * 10)

            rotate_log_if_needed(log_path, 50)

            self.assertTrue(log_path.exists())
            self.assertFalse(log_path.with_suffix(".log.1").exists())

    def test_zero_cap_disables_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "daemon.log"
            log_path.write_bytes(b"x" * 100)

            rotate_log_if_needed(log_path, 0)

            self.assertTrue(log_path.exists())
            self.assertFalse(log_path.with_suffix(".log.1").exists())

    def test_missing_log_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "daemon.log"
            # Must not raise even though the file does not exist.
            rotate_log_if_needed(log_path, 50)
            self.assertFalse(log_path.exists())

    def test_replaces_existing_rotated_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "daemon.log"
            rotated = log_path.with_suffix(".log.1")
            rotated.write_bytes(b"old")
            log_path.write_bytes(b"y" * 100)

            rotate_log_if_needed(log_path, 50)

            self.assertEqual(rotated.read_bytes(), b"y" * 100)


class StalePidTests(unittest.TestCase):
    def test_is_pid_stale_for_dead_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "daemon.pid"
            pid_path.write_text("99999999", encoding="utf-8")
            self.assertTrue(is_pid_stale(pid_path))

    def test_is_pid_stale_for_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "daemon.pid"
            pid_path.write_text(str(os.getpid()), encoding="utf-8")
            self.assertFalse(is_pid_stale(pid_path))

    def test_is_pid_stale_for_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "daemon.pid"
            self.assertFalse(is_pid_stale(pid_path))

    def test_is_pid_stale_for_garbage_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "daemon.pid"
            pid_path.write_text("not-a-pid", encoding="utf-8")
            self.assertFalse(is_pid_stale(pid_path))

    def test_stale_pid_warnings_reports_dead_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            service_paths(config).pid_path.write_text("99999999", encoding="utf-8")
            menubar_service_paths(config).pid_path.write_text(
                str(os.getpid()), encoding="utf-8"
            )

            warnings = stale_pid_warnings(config)

            self.assertEqual(warnings, [("daemon", 99999999)])

    def test_stale_pid_warnings_empty_when_no_pid_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            self.assertEqual(stale_pid_warnings(config), [])


class StopBackgroundProcessTests(unittest.TestCase):
    def test_sigkill_escalation_kills_sigterm_ignoring_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            paths = service_paths(config)
            paths.pid_path.parent.mkdir(parents=True, exist_ok=True)
            # A child that ignores SIGTERM, so only SIGKILL can stop it.
            program = (
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(60)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", program],
                start_new_session=True,
            )
            try:
                paths.pid_path.write_text(str(process.pid), encoding="utf-8")
                # Give the child a moment to install the SIGTERM handler.
                time.sleep(0.3)

                stopped_pid = stop_daemon(config)
                return_code = process.wait(timeout=5)

                self.assertEqual(stopped_pid, process.pid)
                self.assertEqual(return_code, -signal.SIGKILL)
                self.assertFalse(paths.pid_path.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)

    def test_stop_removes_stale_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            paths = service_paths(config)
            paths.pid_path.parent.mkdir(parents=True, exist_ok=True)
            paths.pid_path.write_text("99999999", encoding="utf-8")

            stopped_pid = stop_daemon(config)

            self.assertEqual(stopped_pid, 99999999)
            self.assertFalse(paths.pid_path.exists())

    def test_stop_returns_none_without_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            self.assertIsNone(stop_daemon(config))


class StartBackgroundProcessRotationTests(unittest.TestCase):
    def test_start_rotates_oversized_daemon_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AgentVoiceConfig(
                config_path=Path(tmp) / "config.toml",
                database_path=Path(tmp) / "events.sqlite3",
                max_log_bytes=50,
            )
            paths = service_paths(config)
            paths.log_path.parent.mkdir(parents=True, exist_ok=True)
            paths.log_path.write_bytes(b"z" * 100)

            captured: dict[str, object] = {}

            class _FakeProcess:
                def __init__(self, pid: int) -> None:
                    self.pid = pid

            def _fake_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
                captured["argv"] = argv
                return _FakeProcess(os.getpid())

            original_popen = service.subprocess.Popen
            service.subprocess.Popen = _fake_popen  # type: ignore[assignment]
            try:
                # The real Popen would inherit the log fd; our fake does not, so
                # the parent-side handle is GC'd later. Silence that benign
                # ResourceWarning rather than mask a real leak.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", ResourceWarning)
                    pid = service.start_daemon(config)
                    gc.collect()
            finally:
                service.subprocess.Popen = original_popen  # type: ignore[assignment]

            self.assertEqual(pid, os.getpid())
            self.assertTrue(paths.log_path.with_suffix(".log.1").exists())
            self.assertEqual(captured["argv"], service_python_invocation(config, ["daemon"]))
            self.assertEqual(read_pid(paths.pid_path), os.getpid())


class DaemonStatusTests(unittest.TestCase):
    def test_running_with_live_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            paths = service_paths(config)
            paths.pid_path.parent.mkdir(parents=True, exist_ok=True)
            paths.pid_path.write_text(str(os.getpid()), encoding="utf-8")

            pid, running = daemon_status(config)

            self.assertEqual(pid, os.getpid())
            self.assertTrue(running)

    def test_running_when_only_a_fresh_heartbeat_exists(self) -> None:
        # H4: a launchd-managed daemon may not leave a live pid file. A fresh
        # heartbeat alone must be read as "running" so status/doctor see it up.
        from agent_voice.heartbeat import write_heartbeat

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            # No pid file at all — only a heartbeat written just now.
            write_heartbeat(config)

            pid, running = daemon_status(config)

            self.assertIsNone(pid)
            self.assertTrue(running)

    def test_down_when_heartbeat_is_stale(self) -> None:
        from agent_voice.heartbeat import write_heartbeat

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            # A heartbeat far older than the freshness window.
            write_heartbeat(config, now=time.time() - 10_000)

            pid, running = daemon_status(config)

            self.assertIsNone(pid)
            self.assertFalse(running)

    def test_down_when_no_pid_and_no_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)

            pid, running = daemon_status(config)

            self.assertIsNone(pid)
            self.assertFalse(running)

    def test_down_when_pid_is_dead_and_no_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            paths = service_paths(config)
            paths.pid_path.parent.mkdir(parents=True, exist_ok=True)
            paths.pid_path.write_text("99999999", encoding="utf-8")

            pid, running = daemon_status(config)

            self.assertEqual(pid, 99999999)
            self.assertFalse(running)


if __name__ == "__main__":
    unittest.main()
