import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_voice.cli import _maybe_setup_menubar, _menubar_install_command, main
from agent_voice.config import load_config, set_voice_config, write_default_config
from agent_voice.runtime import set_active_voice_sessions


class UserPromptInterruptTests(unittest.TestCase):
    def _run_user_prompt(self, config_path: Path, session_id: str) -> MagicMock:
        payload = json.dumps({"session_id": session_id})
        mock_stop = MagicMock(return_value=4321)
        with (
            patch("agent_voice.cli.stop_speaking", mock_stop),
            patch("sys.stdin", StringIO(payload)),
            redirect_stdout(StringIO()),
        ):
            main(["--config", str(config_path), "collect", "claude-code", "--hook", "UserPromptSubmit"])
        return mock_stop

    def test_reply_into_active_session_stops_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            config = load_config(config_path)
            set_active_voice_sessions(config, ["sess-1"])  # currently being voiced
            mock_stop = self._run_user_prompt(config_path, "sess-1")
            mock_stop.assert_called_once()

    def test_reply_into_other_session_does_not_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            config = load_config(config_path)
            set_active_voice_sessions(config, ["sess-1"])
            mock_stop = self._run_user_prompt(config_path, "different-session")
            mock_stop.assert_not_called()

    def test_disabled_toggle_never_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            set_voice_config(config_path, interrupt_on_user_input=False)
            config = load_config(config_path)
            set_active_voice_sessions(config, ["sess-1"])
            mock_stop = self._run_user_prompt(config_path, "sess-1")
            mock_stop.assert_not_called()


class CliTests(unittest.TestCase):
    def test_install_claude_code_accepts_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured: dict[str, Path] = {}

            def fake_install(**kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    settings_path=root / "claude-personal" / "settings.json",
                    backup_path=root / "backup.json",
                    wrapper_path=root / "bin" / "hook",
                    config_path=root / "config.toml",
                    database_path=root / "events.sqlite3",
                    installed_events=("Stop",),
                )

            with (
                patch("agent_voice.cli.install_claude_code_personal", fake_install),
                redirect_stdout(StringIO()),
            ):
                main(
                    [
                        "install",
                        "claude-code",
                        "--claude-config-dir",
                        str(root / "claude-personal"),
                    ]
                )

            self.assertEqual(
                captured["settings_path"],
                root / "claude-personal" / "settings.json",
            )

    def test_install_claude_code_accepts_settings_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "custom-settings.json"
            captured: dict[str, Path] = {}

            def fake_install(**kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    settings_path=settings_path,
                    backup_path=Path(tmp) / "backup.json",
                    wrapper_path=Path(tmp) / "bin" / "hook",
                    config_path=Path(tmp) / "config.toml",
                    database_path=Path(tmp) / "events.sqlite3",
                    installed_events=("Stop",),
                )

            with (
                patch("agent_voice.cli.install_claude_code_personal", fake_install),
                redirect_stdout(StringIO()),
            ):
                main(["install", "claude-code", "--settings-path", str(settings_path)])

            self.assertEqual(captured["settings_path"], settings_path)

    def test_install_codex_accepts_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured: dict[str, Path] = {}

            def fake_install(**kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    hooks_path=root / "codex-personal" / "hooks.json",
                    backup_path=root / "backup.json",
                    wrapper_path=root / "bin" / "hook",
                    config_path=root / "config.toml",
                    database_path=root / "events.sqlite3",
                    installed_events=("Stop",),
                )

            with (
                patch("agent_voice.cli.install_codex_personal", fake_install),
                redirect_stdout(StringIO()),
            ):
                main(["install", "codex", "--codex-home", str(root / "codex-personal")])

            self.assertEqual(captured["codex_home"], root / "codex-personal")

    def test_install_codex_accepts_hooks_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "custom-hooks.json"
            captured: dict[str, Path] = {}

            def fake_install(**kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    hooks_path=hooks_path,
                    backup_path=Path(tmp) / "backup.json",
                    wrapper_path=Path(tmp) / "bin" / "hook",
                    config_path=Path(tmp) / "config.toml",
                    database_path=Path(tmp) / "events.sqlite3",
                    installed_events=("Stop",),
                )

            with (
                patch("agent_voice.cli.install_codex_personal", fake_install),
                redirect_stdout(StringIO()),
            ):
                main(["install", "codex", "--hooks-path", str(hooks_path)])

            self.assertEqual(captured["hooks_path"], hooks_path)


class SetupCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep setup tests hermetic: the menu bar step prompts/installs and is
        # covered separately in SetupMenubarTests.
        patcher = patch("agent_voice.cli._maybe_setup_menubar")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_claude(self, **kwargs):
        return SimpleNamespace(settings_path=Path("/tmp/settings.json"))

    def _fake_codex(self, **kwargs):
        return SimpleNamespace(hooks_path=Path("/tmp/hooks.json"))

    def _router_mock(self, *, spoken: bool = True, error: str | None = None) -> MagicMock:
        router = MagicMock()
        router.deliver.return_value = [SimpleNamespace(spoken=spoken, error=error)]
        return router

    def test_setup_with_existing_key_enables_openai_and_installs_both(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            claude_install = MagicMock(side_effect=self._fake_claude)
            codex_install = MagicMock(side_effect=self._fake_codex)
            router = self._router_mock()

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                patch("agent_voice.cli.install_claude_code_personal", claude_install),
                patch("agent_voice.cli.install_codex_personal", codex_install),
                patch("agent_voice.cli.start_daemon", return_value=4321) as start,
                patch("agent_voice.cli.DeliveryRouter", return_value=router),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "setup", "both"])

            config = load_config(config_path)
            self.assertEqual(config.voice_backend, "openai_tts")
            self.assertEqual(config.voice_name, "marin")
            claude_install.assert_called_once()
            codex_install.assert_called_once()
            start.assert_called_once()
            router.deliver.assert_called_once()

    def test_setup_prompts_and_saves_key_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            save_key = MagicMock()

            with (
                patch.dict(os.environ, {}, clear=False),
                patch("agent_voice.cli.get_openai_secret_status", return_value=SimpleNamespace(available=False, source="missing")),
                patch("agent_voice.cli.getpass.getpass", return_value="sk-from-prompt"),
                patch("agent_voice.cli.set_openai_keychain_secret", save_key),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude),
                patch("agent_voice.cli.start_daemon", return_value=1),
                patch("agent_voice.cli.DeliveryRouter", return_value=self._router_mock()),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "setup", "claude-code"])

            save_key.assert_called_once()
            self.assertEqual(save_key.call_args.args[1], "sk-from-prompt")

    def test_setup_claude_only_skips_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            codex_install = MagicMock(side_effect=self._fake_codex)

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude),
                patch("agent_voice.cli.install_codex_personal", codex_install),
                patch("agent_voice.cli.start_daemon", return_value=1),
                patch("agent_voice.cli.DeliveryRouter", return_value=self._router_mock()),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "setup", "claude-code"])

            codex_install.assert_not_called()

    def test_setup_local_uses_macos_say_without_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            getpass_mock = MagicMock()

            with (
                patch("agent_voice.cli.getpass.getpass", getpass_mock),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude),
                patch("agent_voice.cli.start_daemon", return_value=1),
                patch("agent_voice.cli.DeliveryRouter", return_value=self._router_mock()),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "setup", "claude-code", "--local"])

            config = load_config(config_path)
            self.assertEqual(config.voice_backend, "macos_say")
            getpass_mock.assert_not_called()

    def test_setup_no_test_skips_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            router = self._router_mock()

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude),
                patch("agent_voice.cli.start_daemon", return_value=1),
                patch("agent_voice.cli.DeliveryRouter", return_value=router),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "setup", "claude-code", "--no-test"])

            router.deliver.assert_not_called()

    def test_setup_warns_when_test_audio_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            router = self._router_mock(spoken=False, error="say command not found")
            out = StringIO()

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude),
                patch("agent_voice.cli.start_daemon", return_value=1),
                patch("agent_voice.cli.DeliveryRouter", return_value=router),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "setup", "claude-code"])

            self.assertIn("could not play audio", out.getvalue())

    def test_setup_reports_invalid_settings_json(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            def boom(**kwargs):
                raise _json.JSONDecodeError("bad", "doc", 0)

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=boom),
                patch("agent_voice.cli.start_daemon", return_value=1) as start,
                patch("agent_voice.cli.DeliveryRouter", return_value=self._router_mock()),
                redirect_stdout(StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    main(["--config", str(config_path), "setup", "claude-code"])

            start.assert_not_called()


class SetupMenubarTests(unittest.TestCase):
    def _config(self) -> SimpleNamespace:
        return SimpleNamespace(config_path=Path("/tmp/config.toml"))

    def test_choice_false_does_nothing(self) -> None:
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli.start_menubar") as start,
            patch("agent_voice.cli.subprocess.run") as run,
            redirect_stdout(StringIO()),
        ):
            _maybe_setup_menubar(self._config(), choice=False)
        start.assert_not_called()
        run.assert_not_called()

    def test_choice_true_with_cocoa_present_starts_without_install(self) -> None:
        out = StringIO()
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli._cocoa_available", return_value=True),
            patch("agent_voice.cli.subprocess.run") as run,
            patch("agent_voice.cli.start_menubar", return_value=999) as start,
            redirect_stdout(out),
        ):
            _maybe_setup_menubar(self._config(), choice=True)
        run.assert_not_called()
        start.assert_called_once()
        self.assertIn("Menu bar started", out.getvalue())

    def test_choice_true_installs_dependency_then_starts(self) -> None:
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli._cocoa_available", return_value=False),
            patch("agent_voice.cli.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
            patch("agent_voice.cli.start_menubar", return_value=1) as start,
            redirect_stdout(StringIO()),
        ):
            _maybe_setup_menubar(self._config(), choice=True)
        run.assert_called_once()
        start.assert_called_once()

    def test_choice_true_install_failure_skips_start(self) -> None:
        out = StringIO()
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli._cocoa_available", return_value=False),
            patch("agent_voice.cli.subprocess.run", return_value=SimpleNamespace(returncode=1)),
            patch("agent_voice.cli.start_menubar") as start,
            redirect_stdout(out),
        ):
            _maybe_setup_menubar(self._config(), choice=True)
        start.assert_not_called()
        self.assertIn("install failed", out.getvalue())

    def test_choice_none_non_tty_skips(self) -> None:
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli.sys.stdin.isatty", return_value=False),
            patch("agent_voice.cli.start_menubar") as start,
            redirect_stdout(StringIO()),
        ):
            _maybe_setup_menubar(self._config(), choice=None)
        start.assert_not_called()

    def test_choice_none_tty_default_yes_starts(self) -> None:
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value=""),
            patch("agent_voice.cli._cocoa_available", return_value=True),
            patch("agent_voice.cli.start_menubar", return_value=7) as start,
            redirect_stdout(StringIO()),
        ):
            _maybe_setup_menubar(self._config(), choice=None)
        start.assert_called_once()

    def test_choice_none_tty_no_skips(self) -> None:
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="n"),
            patch("agent_voice.cli.start_menubar") as start,
            redirect_stdout(StringIO()),
        ):
            _maybe_setup_menubar(self._config(), choice=None)
        start.assert_not_called()

    def test_non_darwin_skips_even_when_forced(self) -> None:
        out = StringIO()
        with (
            patch("agent_voice.cli.sys.platform", "linux"),
            patch("agent_voice.cli.start_menubar") as start,
            redirect_stdout(out),
        ):
            _maybe_setup_menubar(self._config(), choice=True)
        start.assert_not_called()
        self.assertIn("macOS-only", out.getvalue())

    def test_install_command_prefers_pipx_inject_in_pipx_venv(self) -> None:
        venv = "/home/u/.local/pipx/venvs/voiccce"
        with (
            patch("agent_voice.cli.sys.prefix", venv),
            patch("agent_voice.cli.shutil.which", return_value="/opt/homebrew/bin/pipx"),
        ):
            command = _menubar_install_command()
        self.assertEqual(command, ["pipx", "inject", "voiccce", "pyobjc-framework-Cocoa"])

    def test_install_command_falls_back_to_pip(self) -> None:
        with (
            patch("agent_voice.cli.sys.prefix", "/usr/local"),
            patch("agent_voice.cli.shutil.which", return_value=None),
        ):
            command = _menubar_install_command()
        self.assertEqual(command, [sys.executable, "-m", "pip", "install", "pyobjc-framework-Cocoa"])


if __name__ == "__main__":
    unittest.main()
