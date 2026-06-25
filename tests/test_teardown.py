import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_voice import teardown
from agent_voice.config import AgentVoiceConfig
from agent_voice.installer.claude_code import MARKER as CLAUDE_MARKER
from agent_voice.installer.codex import MARKER as CODEX_MARKER
from agent_voice.installer.pi import MARKER as PI_MARKER


def _make_config(home: Path) -> AgentVoiceConfig:
    return AgentVoiceConfig(
        config_path=home / "config.toml",
        database_path=home / "events.sqlite3",
    )


def _wired_claude_settings() -> dict:
    return {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "bash existing.sh"}]},
                {"hooks": [{"type": "command", "command": f"{CLAUDE_MARKER} /x/voiccce-claude-hook Stop"}]},
            ]
        }
    }


def _wired_codex_hooks() -> dict:
    return {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": f"/usr/bin/env {CODEX_MARKER} /x/voiccce-codex-hook Stop"}]},
            ]
        }
    }


class DetectWiredIntegrationsTests(unittest.TestCase):
    def test_detects_each_wired_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_path = root / "claude" / "settings.json"
            codex_path = root / "codex" / "hooks.json"
            pi_path = root / "pi" / "voiccce.ts"
            for path in (claude_path, codex_path, pi_path):
                path.parent.mkdir(parents=True, exist_ok=True)
            claude_path.write_text(json.dumps(_wired_claude_settings()), encoding="utf-8")
            codex_path.write_text(json.dumps(_wired_codex_hooks()), encoding="utf-8")
            pi_path.write_text(f"// {PI_MARKER} generated extension\n", encoding="utf-8")

            config = _make_config(root / ".voiccce")
            wired = teardown.detect_wired_integrations(
                config,
                claude_settings_path=claude_path,
                codex_hooks_path=codex_path,
                pi_extension_path=pi_path,
            )
            self.assertEqual(wired, ["claude-code", "codex", "pi"])

    def test_detects_subset_and_ignores_non_voiccce_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_path = root / "claude" / "settings.json"
            codex_path = root / "codex" / "hooks.json"
            pi_path = root / "pi" / "voiccce.ts"
            claude_path.parent.mkdir(parents=True, exist_ok=True)
            codex_path.parent.mkdir(parents=True, exist_ok=True)
            # Claude wired, Codex present but no marker, pi missing entirely.
            claude_path.write_text(json.dumps(_wired_claude_settings()), encoding="utf-8")
            codex_path.write_text(
                json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "bash other.sh"}]}]}}),
                encoding="utf-8",
            )

            config = _make_config(root / ".voiccce")
            wired = teardown.detect_wired_integrations(
                config,
                claude_settings_path=claude_path,
                codex_hooks_path=codex_path,
                pi_extension_path=pi_path,
            )
            self.assertEqual(wired, ["claude-code"])

    def test_no_integrations_when_nothing_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _make_config(root / ".voiccce")
            wired = teardown.detect_wired_integrations(
                config,
                claude_settings_path=root / "missing-settings.json",
                codex_hooks_path=root / "missing-hooks.json",
                pi_extension_path=root / "missing.ts",
            )
            self.assertEqual(wired, [])

    def test_pi_extension_without_marker_is_not_wired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pi_path = root / "pi" / "voiccce.ts"
            pi_path.parent.mkdir(parents=True, exist_ok=True)
            pi_path.write_text("// hand-written extension, no marker\n", encoding="utf-8")
            config = _make_config(root / ".voiccce")
            wired = teardown.detect_wired_integrations(
                config,
                claude_settings_path=root / "missing.json",
                codex_hooks_path=root / "missing.json",
                pi_extension_path=pi_path,
            )
            self.assertEqual(wired, [])


class PackageRemovalCommandTests(unittest.TestCase):
    def test_pipx_command_under_pipx_venv(self) -> None:
        fake_prefix = "/home/u/.local/pipx/venvs/voiccce"
        with mock.patch.object(teardown.sys, "prefix", fake_prefix), \
                mock.patch.object(teardown.shutil, "which", return_value="/usr/bin/pipx"):
            self.assertEqual(
                teardown.package_removal_command(),
                ["pipx", "uninstall", "voiccce"],
            )

    def test_pip_command_when_not_pipx(self) -> None:
        fake_prefix = "/usr/local"
        with mock.patch.object(teardown.sys, "prefix", fake_prefix), \
                mock.patch.object(teardown.sys, "executable", "/usr/bin/python3"), \
                mock.patch.object(teardown.shutil, "which", return_value=None):
            self.assertEqual(
                teardown.package_removal_command(),
                ["/usr/bin/python3", "-m", "pip", "uninstall", "-y", "voiccce"],
            )

    def test_pip_command_when_pipx_layout_but_pipx_missing(self) -> None:
        fake_prefix = "/home/u/.local/pipx/venvs/voiccce"
        with mock.patch.object(teardown.sys, "prefix", fake_prefix), \
                mock.patch.object(teardown.sys, "executable", "/usr/bin/python3"), \
                mock.patch.object(teardown.shutil, "which", return_value=None):
            self.assertEqual(
                teardown.package_removal_command(),
                ["/usr/bin/python3", "-m", "pip", "uninstall", "-y", "voiccce"],
            )


class RunTeardownTests(unittest.TestCase):
    def _build_home(self, root: Path) -> Path:
        """Create a fake ~/.voiccce with config, db, logs, and a wrapper script."""
        home = root / ".voiccce"
        (home / "bin").mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text("[meta]\nschema_version = 1\n", encoding="utf-8")
        (home / "events.sqlite3").write_text("db", encoding="utf-8")
        (home / "daemon.log").write_text("log", encoding="utf-8")
        wrapper = home / "bin" / "voiccce-claude-hook"
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        return home

    def _patch_externals(self, *, autostart_present: bool = True):
        """Patch every side-effecting external so nothing touches the real system."""
        status = {
            teardown.launchagent.DAEMON_LABEL: {"plist_present": autostart_present, "loaded": autostart_present},
            teardown.launchagent.MENUBAR_LABEL: {"plist_present": False, "loaded": False},
        }
        patches = {
            "stop_daemon": mock.patch.object(teardown.service, "stop_daemon", return_value=4321),
            "stop_menubar": mock.patch.object(teardown.service, "stop_menubar", return_value=8765),
            "autostart_status": mock.patch.object(teardown.launchagent, "autostart_status", return_value=status),
            "disable_autostart": mock.patch.object(
                teardown.launchagent, "disable_autostart", return_value=[teardown.launchagent.DAEMON_LABEL]
            ),
            "delete_secret": mock.patch.object(
                teardown.secrets, "delete_openai_keychain_secret", return_value=True
            ),
            "claude_remove": mock.patch.object(teardown.claude_code, "remove_claude_code_personal"),
            "codex_remove": mock.patch.object(teardown.codex, "remove_codex_personal"),
            "pi_remove": mock.patch.object(teardown.pi, "remove_pi_personal"),
            "package_command": mock.patch.object(
                teardown, "package_removal_command", return_value=["pip", "uninstall", "-y", "voiccce"]
            ),
        }
        return patches

    def test_full_teardown_keeps_data_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._build_home(root)
            config = _make_config(home)
            patches = self._patch_externals()
            with patches["stop_daemon"], patches["stop_menubar"], patches["autostart_status"], \
                    patches["disable_autostart"] as disable, patches["delete_secret"] as delete_secret, \
                    patches["claude_remove"] as claude_remove, patches["codex_remove"] as codex_remove, \
                    patches["pi_remove"] as pi_remove, patches["package_command"]:
                claude_remove.return_value = mock.Mock(removed_events=("Stop", "Notification"))
                codex_remove.return_value = mock.Mock(removed_events=("Stop",))
                pi_remove.return_value = mock.Mock(extension_removed=True)

                report = teardown.run_teardown(config, teardown.TeardownPlan())

            # Services stopped.
            self.assertIn("daemon", report.stopped)
            self.assertIn("menubar", report.stopped)
            # Autostart disabled.
            disable.assert_called_once()
            self.assertEqual(report.removed_autostart, [teardown.launchagent.DAEMON_LABEL])
            # Each integration unwired.
            claude_remove.assert_called_once()
            codex_remove.assert_called_once()
            pi_remove.assert_called_once()
            self.assertEqual(report.removed_hooks["claude-code"], ["Stop", "Notification"])
            self.assertEqual(report.removed_hooks["codex"], ["Stop"])
            self.assertTrue(report.removed_hooks["pi"])
            # Orphaned wrappers removed (real call against the tempdir home).
            self.assertEqual(len(report.removed_wrappers), 1)
            self.assertFalse((home / "bin" / "voiccce-claude-hook").exists())
            # Keychain delete called.
            delete_secret.assert_called_once_with(config)
            self.assertTrue(report.keychain_deleted)
            # Data kept.
            self.assertFalse(report.data_removed)
            self.assertTrue(home.exists())
            self.assertTrue((home / "config.toml").exists())
            self.assertTrue(any("Kept data directory" in note for note in report.notes))
            self.assertEqual(report.package_command, ["pip", "uninstall", "-y", "voiccce"])

    def test_purge_data_removes_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._build_home(root)
            config = _make_config(home)
            patches = self._patch_externals()
            with patches["stop_daemon"], patches["stop_menubar"], patches["autostart_status"], \
                    patches["disable_autostart"], patches["delete_secret"], \
                    patches["claude_remove"] as claude_remove, patches["codex_remove"] as codex_remove, \
                    patches["pi_remove"] as pi_remove, patches["package_command"]:
                claude_remove.return_value = mock.Mock(removed_events=())
                codex_remove.return_value = mock.Mock(removed_events=())
                pi_remove.return_value = mock.Mock(extension_removed=False)

                report = teardown.run_teardown(config, teardown.TeardownPlan(purge_data=True))

            self.assertTrue(report.data_removed)
            self.assertFalse(home.exists())

    def test_purge_refuses_non_voiccce_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A home that is neither named .voiccce nor holds telltale files.
            home = root / "random-dir"
            home.mkdir(parents=True, exist_ok=True)
            (home / "unrelated.txt").write_text("keep me", encoding="utf-8")
            config = AgentVoiceConfig(
                config_path=home / "settings.cfg",  # not config.toml
                database_path=home / "data.db",  # not events.sqlite3
            )
            patches = self._patch_externals()
            with patches["stop_daemon"], patches["stop_menubar"], patches["autostart_status"], \
                    patches["disable_autostart"], patches["delete_secret"], \
                    patches["claude_remove"] as claude_remove, patches["codex_remove"] as codex_remove, \
                    patches["pi_remove"] as pi_remove, patches["package_command"]:
                claude_remove.return_value = mock.Mock(removed_events=())
                codex_remove.return_value = mock.Mock(removed_events=())
                pi_remove.return_value = mock.Mock(extension_removed=False)

                report = teardown.run_teardown(config, teardown.TeardownPlan(purge_data=True))

            self.assertFalse(report.data_removed)
            self.assertTrue(home.exists())
            self.assertTrue(any("Refusing to purge" in note for note in report.notes))

    def test_restore_backups_invokes_installer_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._build_home(root)
            config = _make_config(home)
            patches = self._patch_externals()
            with patches["stop_daemon"], patches["stop_menubar"], patches["autostart_status"], \
                    patches["disable_autostart"], patches["delete_secret"], \
                    patches["claude_remove"] as claude_remove, patches["codex_remove"] as codex_remove, \
                    patches["pi_remove"] as pi_remove, patches["package_command"], \
                    mock.patch.object(teardown.claude_code, "restore_latest_backup", return_value=Path("/b/claude.bak")) as claude_restore, \
                    mock.patch.object(teardown.codex, "restore_latest_backup", return_value=Path("/b/codex.bak")) as codex_restore:
                claude_remove.return_value = mock.Mock(removed_events=("Stop",))
                codex_remove.return_value = mock.Mock(removed_events=("Stop",))
                pi_remove.return_value = mock.Mock(extension_removed=True)

                report = teardown.run_teardown(
                    config, teardown.TeardownPlan(restore_backups=True)
                )

            claude_restore.assert_called_once()
            codex_restore.assert_called_once()
            self.assertIn("claude-code:/b/claude.bak", report.backups_restored)
            self.assertIn("codex:/b/codex.bak", report.backups_restored)

    def test_stop_services_disabled_skips_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._build_home(root)
            config = _make_config(home)
            patches = self._patch_externals()
            with patches["stop_daemon"] as stop_daemon, patches["stop_menubar"] as stop_menubar, \
                    patches["autostart_status"], patches["disable_autostart"], patches["delete_secret"], \
                    patches["claude_remove"] as claude_remove, patches["codex_remove"] as codex_remove, \
                    patches["pi_remove"] as pi_remove, patches["package_command"]:
                claude_remove.return_value = mock.Mock(removed_events=())
                codex_remove.return_value = mock.Mock(removed_events=())
                pi_remove.return_value = mock.Mock(extension_removed=False)

                report = teardown.run_teardown(
                    config, teardown.TeardownPlan(stop_services=False)
                )

            stop_daemon.assert_not_called()
            stop_menubar.assert_not_called()
            self.assertEqual(report.stopped, [])

    def test_remove_autostart_disabled_skips_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._build_home(root)
            config = _make_config(home)
            patches = self._patch_externals()
            with patches["stop_daemon"], patches["stop_menubar"], patches["autostart_status"], \
                    patches["disable_autostart"] as disable, patches["delete_secret"], \
                    patches["claude_remove"] as claude_remove, patches["codex_remove"] as codex_remove, \
                    patches["pi_remove"] as pi_remove, patches["package_command"]:
                claude_remove.return_value = mock.Mock(removed_events=())
                codex_remove.return_value = mock.Mock(removed_events=())
                pi_remove.return_value = mock.Mock(extension_removed=False)

                report = teardown.run_teardown(
                    config, teardown.TeardownPlan(remove_autostart=False)
                )

            disable.assert_not_called()
            self.assertEqual(report.removed_autostart, [])

    def test_autostart_absent_is_noted_not_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._build_home(root)
            config = _make_config(home)
            patches = self._patch_externals(autostart_present=False)
            with patches["stop_daemon"], patches["stop_menubar"], patches["autostart_status"], \
                    patches["disable_autostart"] as disable, patches["delete_secret"], \
                    patches["claude_remove"] as claude_remove, patches["codex_remove"] as codex_remove, \
                    patches["pi_remove"] as pi_remove, patches["package_command"]:
                claude_remove.return_value = mock.Mock(removed_events=())
                codex_remove.return_value = mock.Mock(removed_events=())
                pi_remove.return_value = mock.Mock(extension_removed=False)

                report = teardown.run_teardown(config, teardown.TeardownPlan())

            disable.assert_not_called()
            self.assertEqual(report.removed_autostart, [])
            self.assertTrue(any("Autostart not installed" in note for note in report.notes))

    def test_targets_subset_only_unwires_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._build_home(root)
            config = _make_config(home)
            patches = self._patch_externals()
            with patches["stop_daemon"], patches["stop_menubar"], patches["autostart_status"], \
                    patches["disable_autostart"], patches["delete_secret"], \
                    patches["claude_remove"] as claude_remove, patches["codex_remove"] as codex_remove, \
                    patches["pi_remove"] as pi_remove, patches["package_command"]:
                claude_remove.return_value = mock.Mock(removed_events=("Stop",))
                codex_remove.return_value = mock.Mock(removed_events=())
                pi_remove.return_value = mock.Mock(extension_removed=False)

                report = teardown.run_teardown(
                    config, teardown.TeardownPlan(targets=("claude-code",))
                )

            claude_remove.assert_called_once()
            codex_remove.assert_not_called()
            pi_remove.assert_not_called()
            self.assertIn("claude-code", report.removed_hooks)
            self.assertNotIn("codex", report.removed_hooks)

    def test_idempotent_when_nothing_installed(self) -> None:
        """A second teardown over an already-clean tempdir must not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".voiccce"
            home.mkdir(parents=True, exist_ok=True)
            (home / "config.toml").write_text("[meta]\n", encoding="utf-8")
            config = _make_config(home)
            patches = self._patch_externals(autostart_present=False)
            with patches["stop_daemon"], patches["stop_menubar"], patches["autostart_status"], \
                    patches["disable_autostart"], patches["delete_secret"], \
                    patches["claude_remove"] as claude_remove, patches["codex_remove"] as codex_remove, \
                    patches["pi_remove"] as pi_remove, patches["package_command"]:
                claude_remove.return_value = mock.Mock(removed_events=())
                codex_remove.return_value = mock.Mock(removed_events=())
                pi_remove.return_value = mock.Mock(extension_removed=False)

                report = teardown.run_teardown(config, teardown.TeardownPlan())

            self.assertEqual(report.removed_wrappers, [])

    def test_stuck_service_is_noted_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._build_home(root)
            config = _make_config(home)
            patches = self._patch_externals()
            with mock.patch.object(teardown.service, "stop_daemon", side_effect=RuntimeError("wedged")), \
                    patches["stop_menubar"], patches["autostart_status"], patches["disable_autostart"], \
                    patches["delete_secret"], patches["claude_remove"] as claude_remove, \
                    patches["codex_remove"] as codex_remove, patches["pi_remove"] as pi_remove, \
                    patches["package_command"]:
                claude_remove.return_value = mock.Mock(removed_events=())
                codex_remove.return_value = mock.Mock(removed_events=())
                pi_remove.return_value = mock.Mock(extension_removed=False)

                report = teardown.run_teardown(config, teardown.TeardownPlan())

            self.assertNotIn("daemon", report.stopped)
            self.assertTrue(any("Could not stop daemon" in note for note in report.notes))

    def test_does_not_import_cli(self) -> None:
        import sys

        self.assertNotIn("agent_voice.cli", sys.modules.get("agent_voice.teardown", teardown).__dict__.values())
        # The module's source must not reference the cli module.
        source = Path(teardown.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import agent_voice.cli", source)
        self.assertNotIn("from agent_voice import cli", source)
        self.assertNotIn("from . import cli", source)
        self.assertNotIn("from .cli", source)


if __name__ == "__main__":
    unittest.main()
