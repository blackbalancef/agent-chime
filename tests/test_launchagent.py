import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_voice.config import AgentVoiceConfig
from agent_voice import launchagent, service
from agent_voice.launchagent import (
    DAEMON_LABEL,
    MENUBAR_LABEL,
    _repo_root,
    autostart_status,
    daemon_spec,
    disable_autostart,
    enable_autostart,
    launch_agents_dir,
    menubar_spec,
    plist_path,
    render_plist,
)


class FakeCompleted:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class FakeRunner:
    """Records each launchctl argv and returns a configurable exit code.

    ``returncodes`` maps the launchctl verb (args[1]) to the exit code to return;
    anything unlisted defaults to 0 (success).
    """

    def __init__(self, returncodes: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.returncodes = returncodes or {}

    def __call__(self, args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(list(args))
        verb = args[1] if len(args) > 1 else ""
        return FakeCompleted(self.returncodes.get(verb, 0))

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls if len(call) > 1]


def _config(tmp: str) -> AgentVoiceConfig:
    return AgentVoiceConfig(
        config_path=Path(tmp) / "config.toml",
        database_path=Path(tmp) / "events.sqlite3",
    )


class RenderPlistTests(unittest.TestCase):
    def test_render_produces_valid_plist(self) -> None:
        xml = render_plist(
            DAEMON_LABEL,
            ["/usr/bin/python3", "-m", "agent_voice", "daemon"],
            stdout_path="/tmp/out.log",
            stderr_path="/tmp/err.log",
        )
        parsed = plistlib.loads(xml.encode("utf-8"))
        self.assertEqual(parsed["Label"], DAEMON_LABEL)
        self.assertEqual(
            parsed["ProgramArguments"],
            ["/usr/bin/python3", "-m", "agent_voice", "daemon"],
        )
        self.assertTrue(parsed["RunAtLoad"])
        self.assertTrue(parsed["KeepAlive"])
        self.assertEqual(parsed["StandardOutPath"], "/tmp/out.log")
        self.assertEqual(parsed["StandardErrorPath"], "/tmp/err.log")

    def test_render_respects_run_at_load_and_keep_alive_flags(self) -> None:
        xml = render_plist(
            MENUBAR_LABEL,
            ["python3"],
            stdout_path="/tmp/o",
            stderr_path="/tmp/e",
            run_at_load=False,
            keep_alive=False,
        )
        parsed = plistlib.loads(xml.encode("utf-8"))
        self.assertFalse(parsed["RunAtLoad"])
        self.assertFalse(parsed["KeepAlive"])

    def test_render_coerces_pathlike_args(self) -> None:
        xml = render_plist(
            DAEMON_LABEL,
            ["python3", Path("/a/b")],
            stdout_path=Path("/tmp/out.log"),
            stderr_path=Path("/tmp/err.log"),
        )
        parsed = plistlib.loads(xml.encode("utf-8"))
        self.assertEqual(parsed["ProgramArguments"], ["python3", "/a/b"])
        self.assertEqual(parsed["StandardOutPath"], "/tmp/out.log")

    def test_render_emits_working_directory_and_environment(self) -> None:
        # M6/L1 regression: launchd runs jobs from "/" with a bare env, so the
        # plist must carry WorkingDirectory + EnvironmentVariables (PYTHONPATH)
        # or `python -m agent_voice` cannot import a source-checkout package.
        xml = render_plist(
            DAEMON_LABEL,
            ["python3", "-m", "agent_voice", "daemon"],
            stdout_path="/tmp/o",
            stderr_path="/tmp/e",
            working_directory="/repo/root",
            environment={"PYTHONPATH": "/repo/root:"},
        )
        parsed = plistlib.loads(xml.encode("utf-8"))
        self.assertEqual(parsed["WorkingDirectory"], "/repo/root")
        self.assertEqual(parsed["EnvironmentVariables"]["PYTHONPATH"], "/repo/root:")

    def test_render_omits_working_directory_and_environment_when_unset(self) -> None:
        # Backwards-compatible: callers that pass neither get a plist without the
        # optional keys, so existing render_plist call sites are unaffected.
        xml = render_plist(
            DAEMON_LABEL,
            ["python3"],
            stdout_path="/tmp/o",
            stderr_path="/tmp/e",
        )
        parsed = plistlib.loads(xml.encode("utf-8"))
        self.assertNotIn("WorkingDirectory", parsed)
        self.assertNotIn("EnvironmentVariables", parsed)


class SpecTests(unittest.TestCase):
    def test_daemon_spec_uses_service_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            label, program_args, stdout, stderr, workdir, env = daemon_spec(config)
            self.assertEqual(label, DAEMON_LABEL)
            self.assertEqual(program_args, service.service_python_invocation(config, ["daemon"]))
            self.assertEqual(stdout, service.service_paths(config).log_path)
            self.assertEqual(stderr, service.service_paths(config).log_path)
            self.assertEqual(workdir, _repo_root())
            self.assertIn("PYTHONPATH", env)
            self.assertIn(str(_repo_root()), env["PYTHONPATH"])

    def test_menubar_spec_uses_service_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            label, program_args, stdout, stderr, workdir, env = menubar_spec(config)
            self.assertEqual(label, MENUBAR_LABEL)
            self.assertEqual(program_args, service.service_python_invocation(config, ["menubar"]))
            self.assertEqual(stdout, service.menubar_service_paths(config).log_path)
            self.assertEqual(workdir, _repo_root())
            self.assertIn("PYTHONPATH", env)


class EnableDisableTests(unittest.TestCase):
    def test_enable_writes_plists_and_bootstraps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            runner = FakeRunner()

            enabled = enable_autostart(config, runner=runner)

            self.assertEqual(set(enabled), {DAEMON_LABEL, MENUBAR_LABEL})
            self.assertTrue(plist_path(DAEMON_LABEL).exists())
            self.assertTrue(plist_path(MENUBAR_LABEL).exists())
            # Each label is loaded via the modern bootstrap verb.
            self.assertEqual(runner.verbs().count("bootstrap"), 2)
            self.assertNotIn("load", runner.verbs())
            # The plist embeds the canonical service invocation.
            parsed = plistlib.loads(plist_path(DAEMON_LABEL).read_bytes())
            self.assertEqual(
                parsed["ProgramArguments"],
                service.service_python_invocation(config, ["daemon"]),
            )

    def test_enable_writes_working_directory_and_pythonpath(self) -> None:
        # M6/L1 regression: the on-disk plist autostart writes must carry the repo
        # root as WorkingDirectory and PYTHONPATH so launchd's bare-env job can
        # import a source-checkout / --dev install, mirroring the manual launcher.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            enable_autostart(config, runner=FakeRunner())

            parsed = plistlib.loads(plist_path(DAEMON_LABEL).read_bytes())
            self.assertEqual(parsed["WorkingDirectory"], str(_repo_root()))
            self.assertIn("PYTHONPATH", parsed["EnvironmentVariables"])
            self.assertTrue(
                parsed["EnvironmentVariables"]["PYTHONPATH"].startswith(
                    str(_repo_root())
                )
            )

    def test_enable_preserves_existing_pythonpath(self) -> None:
        # Mirrors service._start_background_process: any inherited PYTHONPATH must
        # be appended after the repo root, not dropped.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ), mock.patch.dict(os.environ, {"PYTHONPATH": "/existing/path"}):
            config = _config(tmp)
            enable_autostart(config, runner=FakeRunner())

            parsed = plistlib.loads(plist_path(DAEMON_LABEL).read_bytes())
            self.assertEqual(
                parsed["EnvironmentVariables"]["PYTHONPATH"],
                f"{_repo_root()}:/existing/path",
            )

    def test_enable_is_idempotent_when_already_loaded(self) -> None:
        # M7 regression: bootstrap refuses an already-loaded label (exit != 0) and
        # the load fallback fails too, but the job is in fact loaded (print/list
        # succeed). enable must still report the labels as enabled instead of
        # returning [] and making the CLI cry "Could not load any autostart agents".
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            # bootout best-effort succeeds is irrelevant; force the (re)load verbs
            # to fail as launchctl does for an already-loaded job, but report it
            # as loaded via print.
            runner = FakeRunner(returncodes={"bootstrap": 1, "load": 1})

            enabled = enable_autostart(config, runner=runner)

            self.assertEqual(set(enabled), {DAEMON_LABEL, MENUBAR_LABEL})

    def test_enable_boots_out_before_reload_for_fresh_plist(self) -> None:
        # M7: re-enabling must boot the job out first so the reload re-reads the
        # freshly written plist rather than silently keeping the stale one.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            runner = FakeRunner()

            enable_autostart(config, runner=runner)

            verbs = runner.verbs()
            # Each label is booted out before being bootstrapped back in.
            self.assertEqual(verbs.count("bootout"), 2)
            # The first bootout precedes the first bootstrap, so the reload always
            # re-reads the freshly written plist.
            first_bootstrap = next(
                i for i, c in enumerate(runner.calls) if c[1] == "bootstrap"
            )
            first_bootout = next(
                i for i, c in enumerate(runner.calls) if c[1] == "bootout"
            )
            self.assertLess(first_bootout, first_bootstrap)

    def test_enable_without_menubar_only_installs_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            runner = FakeRunner()

            enabled = enable_autostart(config, menubar=False, runner=runner)

            self.assertEqual(enabled, [DAEMON_LABEL])
            self.assertTrue(plist_path(DAEMON_LABEL).exists())
            self.assertFalse(plist_path(MENUBAR_LABEL).exists())

    def test_enable_falls_back_to_load_when_bootstrap_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            runner = FakeRunner(returncodes={"bootstrap": 1})

            enabled = enable_autostart(config, runner=runner)

            self.assertEqual(set(enabled), {DAEMON_LABEL, MENUBAR_LABEL})
            verbs = runner.verbs()
            self.assertEqual(verbs.count("bootstrap"), 2)
            self.assertEqual(verbs.count("load"), 2)

    def test_enable_reports_only_loaded_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            # Bootstrap+load fail AND the job is not already loaded (print+list
            # fail) -> label is written but not "enabled".
            runner = FakeRunner(
                returncodes={"bootstrap": 1, "load": 1, "print": 1, "list": 1}
            )

            enabled = enable_autostart(config, runner=runner)

            self.assertEqual(enabled, [])
            # Plists are still written so a later retry can load them.
            self.assertTrue(plist_path(DAEMON_LABEL).exists())

    def test_enable_does_not_touch_config_managed_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            runner = FakeRunner()
            enable_autostart(config, runner=runner)
            # The CLI owns [autostart].managed; this module must not write config.
            self.assertFalse(config.config_path.exists())

    def test_disable_boots_out_and_removes_plists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            enable_runner = FakeRunner()
            enable_autostart(config, runner=enable_runner)

            disable_runner = FakeRunner()
            removed = disable_autostart(config, runner=disable_runner)

            self.assertEqual(set(removed), {DAEMON_LABEL, MENUBAR_LABEL})
            self.assertFalse(plist_path(DAEMON_LABEL).exists())
            self.assertFalse(plist_path(MENUBAR_LABEL).exists())
            self.assertEqual(disable_runner.verbs().count("bootout"), 2)
            self.assertNotIn("unload", disable_runner.verbs())

    def test_disable_falls_back_to_unload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            enable_autostart(config, runner=FakeRunner())

            disable_runner = FakeRunner(returncodes={"bootout": 1})
            disable_autostart(config, runner=disable_runner)

            verbs = disable_runner.verbs()
            self.assertEqual(verbs.count("bootout"), 2)
            self.assertEqual(verbs.count("unload"), 2)

    def test_disable_is_idempotent_when_nothing_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            runner = FakeRunner()

            removed = disable_autostart(config, runner=runner)

            self.assertEqual(removed, [])
            # Still attempts to unload both labels in case they are orphaned.
            self.assertEqual(runner.verbs().count("bootout"), 2)


class StatusTests(unittest.TestCase):
    def test_status_reports_present_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            enable_autostart(config, runner=FakeRunner())

            status = autostart_status(config, runner=FakeRunner())

            self.assertTrue(status[DAEMON_LABEL]["plist_present"])
            self.assertTrue(status[DAEMON_LABEL]["loaded"])
            self.assertTrue(status[MENUBAR_LABEL]["plist_present"])

    def test_status_reports_absent_when_nothing_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            # print + list both fail -> not loaded.
            runner = FakeRunner(returncodes={"print": 1, "list": 1})

            status = autostart_status(config, runner=runner)

            self.assertFalse(status[DAEMON_LABEL]["plist_present"])
            self.assertFalse(status[DAEMON_LABEL]["loaded"])

    def test_status_falls_back_to_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            runner = FakeRunner(returncodes={"print": 1, "list": 0})

            status = autostart_status(config, runner=runner)

            self.assertTrue(status[DAEMON_LABEL]["loaded"])
            self.assertIn("list", runner.verbs())

    def test_status_handles_missing_launchctl(self) -> None:
        def _raising_runner(args, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError("launchctl not found")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            config = _config(tmp)
            status = autostart_status(config, runner=_raising_runner)
            self.assertFalse(status[DAEMON_LABEL]["loaded"])


class PathTests(unittest.TestCase):
    def test_launch_agents_dir_and_plist_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            launchagent.Path, "home", return_value=Path(tmp)
        ):
            self.assertEqual(launch_agents_dir(), Path(tmp) / "Library" / "LaunchAgents")
            self.assertEqual(
                plist_path(DAEMON_LABEL),
                Path(tmp) / "Library" / "LaunchAgents" / f"{DAEMON_LABEL}.plist",
            )


if __name__ == "__main__":
    unittest.main()
