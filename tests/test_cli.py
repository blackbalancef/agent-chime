import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_voice.cli import (
    _apply_stop_hotkey,
    _maybe_setup_menubar,
    _menubar_install_command,
    _resolve_update_source,
    _resolve_menubar_choice,
    _resolve_setup_targets,
    _resolve_setup_language,
    _resolve_stop_hotkey,
    _resolve_voice_backend,
    _test_remediation_hint,
    _update_install_command,
    build_parser,
    format_bytes,
    main,
)
from agent_voice.config import load_config, set_voice_config, write_default_config
from agent_voice.doctor import CheckResult
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
        # Keep setup tests hermetic: the menu bar step installs/starts and is
        # covered separately in MenubarSetupTests.
        patcher = patch("agent_voice.cli._maybe_setup_menubar")
        patcher.start()
        self.addCleanup(patcher.stop)
        validation_patcher = patch(
            "agent_voice.cli.validate_openai_tts_key",
            return_value=SimpleNamespace(ok=True, error=None),
        )
        self.validate_key = validation_patcher.start()
        self.addCleanup(validation_patcher.stop)

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
                patch(
                    "agent_voice.cli.resolve_openai_api_key",
                    return_value=(None, SimpleNamespace(available=False, source="missing")),
                ),
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
            self.validate_key.assert_called()

    def test_setup_rejects_invalid_entered_openai_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            save_key = MagicMock()
            self.validate_key.return_value = SimpleNamespace(ok=False, error="HTTP 401: nope")

            with (
                patch(
                    "agent_voice.cli.resolve_openai_api_key",
                    return_value=(None, SimpleNamespace(available=False, source="missing")),
                ),
                patch("agent_voice.cli.getpass.getpass", return_value="sk-bad"),
                patch("agent_voice.cli.set_openai_keychain_secret", save_key),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude) as install,
                patch("agent_voice.cli.start_daemon") as start,
                redirect_stdout(StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(["--config", str(config_path), "setup", "claude-code"])

            self.assertIn("validation failed", str(raised.exception))
            save_key.assert_not_called()
            install.assert_not_called()
            start.assert_not_called()

    def test_setup_rejects_invalid_existing_key_non_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            self.validate_key.return_value = SimpleNamespace(ok=False, error="HTTP 401: nope")

            with (
                patch(
                    "agent_voice.cli.resolve_openai_api_key",
                    return_value=("sk-bad", SimpleNamespace(available=True, source="keychain")),
                ),
                patch("agent_voice.cli._interactive", return_value=False),
                patch("agent_voice.cli.getpass.getpass") as prompt,
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude) as install,
                redirect_stdout(StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(["--config", str(config_path), "setup", "claude-code"])

            self.assertIn("--reset-key", str(raised.exception))
            prompt.assert_not_called()
            install.assert_not_called()

    def test_setup_saves_explicit_hotkey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude),
                patch("agent_voice.cli.start_daemon", return_value=1),
                patch("agent_voice.cli.DeliveryRouter", return_value=self._router_mock()),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "setup", "claude-code", "--hotkey", "ctrl+alt+cmd+s"])

            config = load_config(config_path)
            self.assertTrue(config.hotkey_enabled)
            self.assertEqual(config.hotkey_stop_speaking, "ctrl+alt+cmd+s")

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

    def test_setup_interactive_picks_macos_skips_key(self) -> None:
        # When the voice picker returns the macOS backend, no key is requested.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            getpass_mock = MagicMock()

            with (
                patch("agent_voice.cli.getpass.getpass", getpass_mock),
                patch("agent_voice.cli._resolve_voice_backend", return_value="macos_say"),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude),
                patch("agent_voice.cli.start_daemon", return_value=1),
                patch("agent_voice.cli.DeliveryRouter", return_value=self._router_mock()),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "setup", "claude-code"])

            config = load_config(config_path)
            self.assertEqual(config.voice_backend, "macos_say")
            getpass_mock.assert_not_called()

    def test_setup_openai_flag_uses_openai_tts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude),
                patch("agent_voice.cli.start_daemon", return_value=1),
                patch("agent_voice.cli.DeliveryRouter", return_value=self._router_mock()),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "setup", "claude-code", "--openai"])

            config = load_config(config_path)
            self.assertEqual(config.voice_backend, "openai_tts")
            self.assertEqual(config.voice_name, "marin")

    def test_setup_cancel_after_voice_choice_does_not_prompt_for_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            key_status = MagicMock(return_value=SimpleNamespace(available=False, source="missing"))

            with (
                patch("agent_voice.cli._resolve_setup_language", return_value=None),
                patch("agent_voice.cli._resolve_voice_backend", return_value="openai_tts"),
                patch("agent_voice.cli._resolve_menubar_choice", side_effect=SystemExit(0)),
                patch("agent_voice.cli.resolve_openai_api_key", key_status),
                patch("agent_voice.cli.getpass.getpass") as getpass_mock,
                redirect_stdout(StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    main(["--config", str(config_path), "setup", "claude-code"])

            key_status.assert_not_called()
            getpass_mock.assert_not_called()

    def test_setup_language_flag_saves_custom_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                patch("agent_voice.cli.install_claude_code_personal", side_effect=self._fake_claude),
                patch("agent_voice.cli.start_daemon", return_value=1),
                patch("agent_voice.cli.DeliveryRouter", return_value=self._router_mock()),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "setup", "claude-code", "--language", "Spanish"])

            self.assertEqual(load_config(config_path).language, "Spanish")

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


class ResolveSetupTargetsTests(unittest.TestCase):
    def test_explicit_both_maps_to_claude_and_codex(self):
        self.assertEqual(_resolve_setup_targets("both"), {"claude-code", "codex"})

    def test_explicit_single_target(self):
        self.assertEqual(_resolve_setup_targets("pi"), {"pi"})
        self.assertEqual(_resolve_setup_targets("claude-code"), {"claude-code"})

    def test_omitted_non_tty_defaults_to_claude_and_codex(self):
        with patch("agent_voice.cli.sys.stdin.isatty", return_value=False):
            self.assertEqual(_resolve_setup_targets(None), {"claude-code", "codex"})

    def test_omitted_tty_runs_picker(self):
        with (
            patch("agent_voice.cli.sys.stdin.isatty", return_value=True),
            patch("agent_voice.cli.sys.stdout.isatty", return_value=True),
            patch("agent_voice.cli.checkbox_select", return_value=["codex", "pi"]) as picker,
        ):
            result = _resolve_setup_targets(None)
        self.assertEqual(result, {"codex", "pi"})
        picker.assert_called_once()
        # default selection preserves the legacy "both" pre-check
        self.assertEqual(picker.call_args.kwargs["default"], ["claude-code", "codex"])

    def test_omitted_tty_cancel_exits(self):
        with (
            patch("agent_voice.cli.sys.stdin.isatty", return_value=True),
            patch("agent_voice.cli.sys.stdout.isatty", return_value=True),
            patch("agent_voice.cli.checkbox_select", return_value=None),
        ):
            with self.assertRaises(SystemExit):
                _resolve_setup_targets(None)


class ResolveSetupLanguageTests(unittest.TestCase):
    def _args(self, language=None) -> SimpleNamespace:
        return SimpleNamespace(language=language)

    def test_explicit_language_flag_returns_value(self):
        with patch("builtins.input") as prompt:
            self.assertEqual(_resolve_setup_language(self._args("Spanish"), default="en"), "Spanish")
        prompt.assert_not_called()

    def test_non_tty_leaves_existing_config(self):
        with (
            patch("agent_voice.cli._interactive", return_value=False),
            patch("builtins.input") as prompt,
        ):
            self.assertIsNone(_resolve_setup_language(self._args(), default="en"))
        prompt.assert_not_called()

    def test_tty_accepts_typed_language(self):
        with (
            patch("agent_voice.cli._interactive", return_value=True),
            patch("builtins.input", return_value="Spanish") as prompt,
        ):
            self.assertEqual(_resolve_setup_language(self._args(), default="en"), "Spanish")
        prompt.assert_called_once()

    def test_tty_empty_input_keeps_default(self):
        with (
            patch("agent_voice.cli._interactive", return_value=True),
            patch("builtins.input", return_value=""),
        ):
            self.assertEqual(_resolve_setup_language(self._args(), default="ru"), "ru")


class ResolveVoiceBackendTests(unittest.TestCase):
    def _args(self, *, local: bool = False, openai: bool = False) -> SimpleNamespace:
        return SimpleNamespace(local=local, openai=openai)

    def test_local_flag_returns_macos_say(self):
        self.assertEqual(_resolve_voice_backend(self._args(local=True)), "macos_say")

    def test_openai_flag_returns_openai_tts(self):
        self.assertEqual(_resolve_voice_backend(self._args(openai=True)), "openai_tts")

    def test_non_tty_defaults_to_openai(self):
        with patch("agent_voice.cli._interactive", return_value=False):
            self.assertEqual(_resolve_voice_backend(self._args()), "openai_tts")

    def test_tty_runs_picker(self):
        with (
            patch("agent_voice.cli._interactive", return_value=True),
            patch("agent_voice.cli.select_one", return_value="macos_say") as picker,
        ):
            result = _resolve_voice_backend(self._args())
        self.assertEqual(result, "macos_say")
        picker.assert_called_once()
        self.assertEqual(picker.call_args.kwargs["default"], "openai_tts")

    def test_tty_cancel_exits(self):
        with (
            patch("agent_voice.cli._interactive", return_value=True),
            patch("agent_voice.cli.select_one", return_value=None),
        ):
            with self.assertRaises(SystemExit):
                _resolve_voice_backend(self._args())


class SetupParserTests(unittest.TestCase):
    def test_local_and_openai_are_mutually_exclusive(self):
        # argparse must reject choosing both voice backends at once.
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            build_parser().parse_args(["setup", "--local", "--openai"])


class ResolveMenubarChoiceTests(unittest.TestCase):
    def _args(self, menubar) -> SimpleNamespace:
        return SimpleNamespace(menubar=menubar)

    def test_explicit_true_and_false(self):
        self.assertIs(_resolve_menubar_choice(self._args(True)), True)
        self.assertIs(_resolve_menubar_choice(self._args(False)), False)

    def test_non_darwin_returns_none(self):
        with patch("agent_voice.cli.sys.platform", "linux"):
            self.assertIsNone(_resolve_menubar_choice(self._args(None)))

    def test_darwin_non_tty_returns_none(self):
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli._interactive", return_value=False),
        ):
            self.assertIsNone(_resolve_menubar_choice(self._args(None)))

    def test_darwin_tty_yes(self):
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli._interactive", return_value=True),
            patch("agent_voice.cli.select_one", return_value="yes") as ask,
        ):
            self.assertIs(_resolve_menubar_choice(self._args(None)), True)
        ask.assert_called_once()

    def test_darwin_tty_no(self):
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli._interactive", return_value=True),
            patch("agent_voice.cli.select_one", return_value="no"),
        ):
            self.assertIs(_resolve_menubar_choice(self._args(None)), False)

    def test_darwin_tty_cancel_exits(self):
        # esc must abort the whole wizard, like the agents and voice menus.
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli._interactive", return_value=True),
            patch("agent_voice.cli.select_one", return_value=None),
        ):
            with self.assertRaises(SystemExit):
                _resolve_menubar_choice(self._args(None))


class ResolveStopHotkeyTests(unittest.TestCase):
    def _args(self, hotkey=None) -> SimpleNamespace:
        return SimpleNamespace(hotkey=hotkey)

    def test_explicit_flag_bypasses_prompt(self):
        with patch("agent_voice.cli.select_one") as picker:
            self.assertEqual(
                _resolve_stop_hotkey(self._args("ctrl+alt+cmd+s"), menubar_enabled=True),
                "ctrl+alt+cmd+s",
            )
        picker.assert_not_called()

    def test_no_menubar_returns_none(self):
        with patch("agent_voice.cli.select_one") as picker:
            self.assertIsNone(_resolve_stop_hotkey(self._args(), menubar_enabled=False))
        picker.assert_not_called()

    def test_non_darwin_returns_none(self):
        with (
            patch("agent_voice.cli.sys.platform", "linux"),
            patch("agent_voice.cli._interactive", return_value=True),
            patch("agent_voice.cli.select_one") as picker,
        ):
            self.assertIsNone(_resolve_stop_hotkey(self._args(), menubar_enabled=True))
        picker.assert_not_called()

    def test_non_tty_returns_none(self):
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli._interactive", return_value=False),
            patch("agent_voice.cli.select_one") as picker,
        ):
            self.assertIsNone(_resolve_stop_hotkey(self._args(), menubar_enabled=True))
        picker.assert_not_called()

    def test_darwin_tty_runs_picker(self):
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli._interactive", return_value=True),
            patch("agent_voice.cli.select_one", return_value="ctrl+alt+cmd+.") as picker,
        ):
            result = _resolve_stop_hotkey(self._args(), menubar_enabled=True)
        self.assertEqual(result, "ctrl+alt+cmd+.")
        picker.assert_called_once()
        self.assertEqual(picker.call_args.kwargs["default"], "alt+cmd+s")

    def test_picker_cancel_exits(self):
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli._interactive", return_value=True),
            patch("agent_voice.cli.select_one", return_value=None),
        ):
            with self.assertRaises(SystemExit):
                _resolve_stop_hotkey(self._args(), menubar_enabled=True)


class ApplyStopHotkeyTests(unittest.TestCase):
    def test_none_leaves_config_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with redirect_stdout(StringIO()):
                _apply_stop_hotkey(config_path, None)
            config = load_config(config_path)
            self.assertTrue(config.hotkey_enabled)
            self.assertEqual(config.hotkey_stop_speaking, "alt+cmd+s")

    def test_spec_is_persisted_canonically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with redirect_stdout(StringIO()):
                _apply_stop_hotkey(config_path, "Command+Option+S")
            config = load_config(config_path)
            self.assertTrue(config.hotkey_enabled)
            self.assertEqual(config.hotkey_stop_speaking, "alt+cmd+s")

    def test_off_disables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with redirect_stdout(StringIO()):
                _apply_stop_hotkey(config_path, "off")
            self.assertFalse(load_config(config_path).hotkey_enabled)

    def test_invalid_spec_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with redirect_stdout(StringIO()), self.assertRaises(SystemExit):
                _apply_stop_hotkey(config_path, "cmd+nope")

    def test_all_off_tokens_disable(self) -> None:
        from agent_voice.cli import _HOTKEY_OFF_TOKENS

        for token in sorted(t for t in _HOTKEY_OFF_TOKENS if t):
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "config.toml"
                set_voice_config(config_path)  # materialize a config first
                with redirect_stdout(StringIO()):
                    _apply_stop_hotkey(config_path, token)
                self.assertFalse(load_config(config_path).hotkey_enabled, token)


class ConfigCommandHotkeyTests(unittest.TestCase):
    def _run_config(self, config_path: Path, *args: str) -> str:
        out = StringIO()
        with redirect_stdout(out):
            main(["--config", str(config_path), "config", *args])
        return out.getvalue()

    def test_set_custom_language_persists_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            output = self._run_config(config_path, "--language", "Spanish")
            self.assertEqual(load_config(config_path).language, "Spanish")
            self.assertIn("Language: Spanish", output)

    def test_set_valid_hotkey_persists_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            output = self._run_config(config_path, "--hotkey", "Command+Option+S")
            config = load_config(config_path)
            self.assertTrue(config.hotkey_enabled)
            self.assertEqual(config.hotkey_stop_speaking, "alt+cmd+s")
            self.assertIn("Stop-speaking hotkey: ⌥⌘S", output)

    def test_off_disables_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            output = self._run_config(config_path, "--hotkey", "off")
            self.assertFalse(load_config(config_path).hotkey_enabled)
            self.assertIn("Stop-speaking hotkey: off", output)

    def test_invalid_hotkey_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with self.assertRaises(SystemExit), redirect_stdout(StringIO()):
                main(["--config", str(config_path), "config", "--hotkey", "cmd+nope"])


class SecretCommandTests(unittest.TestCase):
    def test_secret_set_validates_before_saving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            save_key = MagicMock()

            with (
                patch("agent_voice.cli.getpass.getpass", return_value="sk-good"),
                patch(
                    "agent_voice.cli.validate_openai_tts_key",
                    return_value=SimpleNamespace(ok=True, error=None),
                ) as validate,
                patch("agent_voice.cli.set_openai_keychain_secret", save_key),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "secret", "set", "openai"])

            validate.assert_called_once()
            save_key.assert_called_once()
            self.assertEqual(save_key.call_args.args[1], "sk-good")

    def test_secret_set_rejects_invalid_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            save_key = MagicMock()

            with (
                patch("agent_voice.cli.getpass.getpass", return_value="sk-bad"),
                patch(
                    "agent_voice.cli.validate_openai_tts_key",
                    return_value=SimpleNamespace(ok=False, error="HTTP 401: nope"),
                ),
                patch("agent_voice.cli.set_openai_keychain_secret", save_key),
                redirect_stdout(StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    main(["--config", str(config_path), "secret", "set", "openai"])

            save_key.assert_not_called()


class UpdateCommandTests(unittest.TestCase):
    def _source_checkout(self, root: Path) -> Path:
        source = root / "voiccce"
        source.mkdir()
        (source / "pyproject.toml").write_text("[project]\nname = \"voiccce\"\n", encoding="utf-8")
        (source / "agent_voice").mkdir()
        return source

    def test_update_install_command_uses_pipx_install_force_inside_pipx_venv(self) -> None:
        # A pipx-managed venv reinstalls the whole app non-editably, letting pip
        # resolve dependencies (the old unconditional --no-deps was dropped).
        source = "/work/voiccce"
        with (
            patch("agent_voice.cli.sys.prefix", "/Users/me/.local/pipx/venvs/voiccce"),
            patch("agent_voice.cli.shutil.which", return_value="/opt/homebrew/bin/pipx"),
        ):
            command = _update_install_command(source)

        self.assertEqual(command, ["pipx", "install", "--force", source])

    def test_update_install_command_git_url_uses_pipx_install_force(self) -> None:
        target = "git+https://github.com/blackbalancef/voiccce@main"
        with (
            patch("agent_voice.cli.sys.prefix", "/Users/me/.local/pipx/venvs/voiccce"),
            patch("agent_voice.cli.shutil.which", return_value="/opt/homebrew/bin/pipx"),
        ):
            command = _update_install_command(target)

        self.assertEqual(command, ["pipx", "install", "--force", target])

    def test_update_install_command_dev_uses_editable_runpip(self) -> None:
        # --dev forces an editable install via runpip even inside a pipx venv.
        source = "/work/voiccce"
        with (
            patch("agent_voice.cli.sys.prefix", "/Users/me/.local/pipx/venvs/voiccce"),
            patch("agent_voice.cli.shutil.which", return_value="/opt/homebrew/bin/pipx"),
        ):
            command = _update_install_command(source, editable=True)

        self.assertEqual(
            command,
            ["pipx", "runpip", "voiccce", "install", "--force-reinstall", "-e", source],
        )

    def test_update_install_command_falls_back_to_current_python(self) -> None:
        source = "/work/voiccce"
        with (
            patch("agent_voice.cli.sys.prefix", "/tmp/venv"),
            patch("agent_voice.cli.shutil.which", return_value=None),
        ):
            command = _update_install_command(source)

        self.assertEqual(
            command,
            [sys.executable, "-m", "pip", "install", "--force-reinstall", source],
        )

    def test_resolve_update_source_accepts_explicit_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source_checkout(Path(tmp))
            self.assertEqual(_resolve_update_source(str(source)), source.resolve())

    def test_resolve_update_source_uses_installed_direct_url_when_not_in_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source_checkout(root)
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            with (
                patch("agent_voice.cli.Path.cwd", return_value=elsewhere),
                patch("agent_voice.cli._installed_source_path", return_value=source),
            ):
                self.assertEqual(_resolve_update_source(None), source.resolve())

    def test_resolve_update_source_returns_none_when_no_checkout(self) -> None:
        # No --source, not in a checkout, and no recorded install source → None,
        # so the caller updates from the git URL instead.
        with tempfile.TemporaryDirectory() as tmp:
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            with (
                patch("agent_voice.cli.Path.cwd", return_value=elsewhere),
                patch("agent_voice.cli._installed_source_path", return_value=None),
            ):
                self.assertIsNone(_resolve_update_source(None))

    def test_update_from_git_when_no_local_checkout(self) -> None:
        # With no checkout the update installs from git+url@ref via the build command.
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with (
                patch("agent_voice.cli._resolve_update_source", return_value=None),
                patch("agent_voice.cli._update_install_command", return_value=["install"]) as command,
                patch("agent_voice.cli.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
                patch("agent_voice.cli.daemon_status", return_value=(None, False)),
                patch("agent_voice.cli.menubar_status", return_value=(None, False)),
                redirect_stdout(StringIO()),
            ):
                main(
                    [
                        "--config",
                        str(config_path),
                        "update",
                        "--ref",
                        "v2",
                        "--no-hooks",
                        "--no-probe",
                    ]
                )

            command.assert_called_once_with(
                "git+https://github.com/blackbalancef/voiccce@v2", editable=False
            )
            run.assert_called_once_with(["install"], cwd=None)

    def test_update_restarts_running_services_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source_checkout(root)
            config_path = root / "config.toml"

            with (
                patch("agent_voice.cli._update_install_command", return_value=["install"]) as command,
                patch("agent_voice.cli.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
                patch("agent_voice.cli._reapply_wired_hooks") as reapply,
                patch("agent_voice.cli._health_probe") as probe,
                patch("agent_voice.cli.daemon_status", return_value=(111, True)),
                patch("agent_voice.cli.menubar_status", return_value=(222, True)),
                patch("agent_voice.cli.stop_daemon", return_value=111) as stop_daemon_mock,
                patch("agent_voice.cli.start_daemon", return_value=333) as start_daemon_mock,
                patch("agent_voice.cli.stop_menubar", return_value=222) as stop_menubar_mock,
                patch("agent_voice.cli.start_menubar", return_value=444) as start_menubar_mock,
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "update", "--source", str(source)])

            command.assert_called_once_with(str(source.resolve()), editable=False)
            run.assert_called_once_with(["install"], cwd=str(source.resolve()))
            reapply.assert_called_once()
            probe.assert_called_once()
            stop_daemon_mock.assert_called_once()
            start_daemon_mock.assert_called_once()
            stop_menubar_mock.assert_called_once()
            start_menubar_mock.assert_called_once()

    def test_update_no_hooks_no_probe_skip_reapply_and_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source_checkout(root)
            config_path = root / "config.toml"

            with (
                patch("agent_voice.cli._update_install_command", return_value=["install"]),
                patch("agent_voice.cli.subprocess.run", return_value=SimpleNamespace(returncode=0)),
                patch("agent_voice.cli._reapply_wired_hooks") as reapply,
                patch("agent_voice.cli._health_probe") as probe,
                patch("agent_voice.cli.daemon_status", return_value=(None, False)),
                patch("agent_voice.cli.menubar_status", return_value=(None, False)),
                redirect_stdout(StringIO()),
            ):
                main(
                    [
                        "--config",
                        str(config_path),
                        "update",
                        "--source",
                        str(source),
                        "--no-hooks",
                        "--no-probe",
                    ]
                )

            reapply.assert_not_called()
            probe.assert_not_called()

    def test_update_failure_does_not_restart_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source_checkout(root)
            config_path = root / "config.toml"

            with (
                patch("agent_voice.cli._update_install_command", return_value=["install"]),
                patch("agent_voice.cli.subprocess.run", return_value=SimpleNamespace(returncode=2)),
                patch("agent_voice.cli.daemon_status", return_value=(111, True)),
                patch("agent_voice.cli.menubar_status", return_value=(222, True)),
                patch("agent_voice.cli.stop_daemon") as stop_daemon_mock,
                patch("agent_voice.cli.stop_menubar") as stop_menubar_mock,
                redirect_stdout(StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    main(["--config", str(config_path), "update", "--source", str(source)])

            stop_daemon_mock.assert_not_called()
            stop_menubar_mock.assert_not_called()


class MenubarSetupTests(unittest.TestCase):
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

    def test_choice_none_skips(self) -> None:
        # None means "no decision" (non-macOS or non-interactive); the prompt is
        # now handled up front by _resolve_menubar_choice, so this never installs.
        with (
            patch("agent_voice.cli.sys.platform", "darwin"),
            patch("agent_voice.cli.start_menubar") as start,
            patch("agent_voice.cli.subprocess.run") as run,
            redirect_stdout(StringIO()),
        ):
            _maybe_setup_menubar(self._config(), choice=None)
        start.assert_not_called()
        run.assert_not_called()

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


class VersionTests(unittest.TestCase):
    def test_version_flag_prints_version_and_exits(self) -> None:
        out = StringIO()
        with patch("agent_voice.cli._resolve_version", return_value="9.9.9"):
            parser = build_parser()
            with redirect_stdout(out), self.assertRaises(SystemExit) as raised:
                parser.parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("9.9.9", out.getvalue())

    def test_short_version_flag(self) -> None:
        out = StringIO()
        with patch("agent_voice.cli._resolve_version", return_value="1.2.3"):
            parser = build_parser()
            with redirect_stdout(out), self.assertRaises(SystemExit):
                parser.parse_args(["-V"])
        self.assertIn("1.2.3", out.getvalue())


class DoctorCommandTests(unittest.TestCase):
    def _results(self, *, ok: bool) -> list[CheckResult]:
        return [
            CheckResult(name="config", ok=True, detail="loaded"),
            CheckResult(name="hooks", ok=ok, detail="no agents wired", hint="run setup"),
        ]

    def test_doctor_prints_pass_fail_and_exits_nonzero_on_failure(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with (
                patch("agent_voice.cli.run_doctor", return_value=self._results(ok=False)),
                redirect_stdout(out),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(["--config", str(config_path), "doctor"])
        self.assertEqual(raised.exception.code, 1)
        output = out.getvalue()
        self.assertIn("[PASS] config", output)
        self.assertIn("[FAIL] hooks", output)
        self.assertIn("hint: run setup", output)

    def test_doctor_ok_exits_zero(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with (
                patch("agent_voice.cli.run_doctor", return_value=self._results(ok=True)) as run_doctor,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "doctor", "--no-validate-key"])
            self.assertFalse(run_doctor.call_args.kwargs["validate_key"])

    def test_doctor_json_is_machine_readable(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with (
                patch("agent_voice.cli.run_doctor", return_value=self._results(ok=True)),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "doctor", "--json"])
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual({c["name"] for c in payload["checks"]}, {"config", "hooks"})


class LogsCommandTests(unittest.TestCase):
    def test_logs_friendly_message_when_missing(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with redirect_stdout(out):
                main(["--config", str(config_path), "logs"])
        self.assertIn("No daemon log yet", out.getvalue())

    def test_logs_tails_last_n_lines(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            log_path = config_path.parent / "daemon.log"
            log_path.write_text("\n".join(f"line{i}" for i in range(10)) + "\n", encoding="utf-8")
            with redirect_stdout(out):
                main(["--config", str(config_path), "logs", "-n", "3"])
        output = out.getvalue()
        self.assertIn("line9", output)
        self.assertIn("line7", output)
        self.assertNotIn("line6", output)

    def test_logs_summary_source_uses_pipeline_log(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            (config_path.parent / "summary.log").write_text("summary line\n", encoding="utf-8")
            with redirect_stdout(out):
                main(["--config", str(config_path), "logs", "--summary"])
        self.assertIn("summary line", out.getvalue())


class PruneCommandTests(unittest.TestCase):
    def test_prune_removes_old_processed_events(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli.prune_processed_events", return_value=5) as prune,
                patch("agent_voice.cli.vacuum_db"),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "prune", "--older-than", "30d"])
            prune.assert_called_once()
            cutoff = prune.call_args.kwargs["older_than_epoch"]
            self.assertLess(cutoff, int(__import__("time").time()))
        self.assertIn("Pruned 5 processed event", out.getvalue())

    def test_prune_invalid_age_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with redirect_stdout(StringIO()), self.assertRaises(SystemExit):
                main(["--config", str(config_path), "prune", "--older-than", "nonsense"])


class ClearCommandTests(unittest.TestCase):
    def test_clear_events_with_yes(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli.clear_events", return_value=3) as clear_events,
                patch("agent_voice.cli.clear_notifications") as clear_notifications,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "clear", "--events", "--yes"])
            clear_events.assert_called_once()
            clear_notifications.assert_not_called()
        self.assertIn("Cleared 3 event", out.getvalue())

    def test_clear_all_truncates_pipeline_log(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli.clear_events", return_value=1),
                patch("agent_voice.cli.clear_notifications", return_value=2),
                patch("agent_voice.cli.clear_session_states", return_value=4),
                patch("agent_voice.cli.truncate_pipeline_log") as truncate,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "clear", "--all", "--yes"])
            truncate.assert_called_once()
        self.assertIn("pipeline log", out.getvalue())

    def test_clear_nothing_selected_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with redirect_stdout(StringIO()), self.assertRaises(SystemExit):
                main(["--config", str(config_path), "clear"])

    def test_clear_aborts_when_confirm_declined(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli._interactive", return_value=True),
                patch("agent_voice.cli.confirm", return_value=False),
                patch("agent_voice.cli.clear_events") as clear_events,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "clear", "--events"])
            clear_events.assert_not_called()
        self.assertIn("Aborted", out.getvalue())

    def test_clear_all_non_tty_without_yes_refuses(self) -> None:
        # Irreversible: a non-interactive run (script/pipe/CI/cron) without --yes
        # must refuse and exit non-zero, never silently wipe data.
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli._interactive", return_value=False),
                patch("agent_voice.cli.clear_events") as clear_events,
                patch("agent_voice.cli.clear_notifications") as clear_notifications,
                patch("agent_voice.cli.clear_session_states") as clear_sessions,
                redirect_stdout(out),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(["--config", str(config_path), "clear", "--all"])
            self.assertEqual(raised.exception.code, 2)
            clear_events.assert_not_called()
            clear_notifications.assert_not_called()
            clear_sessions.assert_not_called()
        self.assertIn("Refusing", out.getvalue())
        self.assertIn("--yes", out.getvalue())

    def test_clear_all_non_tty_with_yes_proceeds(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli._interactive", return_value=False),
                patch("agent_voice.cli.clear_events", return_value=1) as clear_events,
                patch("agent_voice.cli.clear_notifications", return_value=2),
                patch("agent_voice.cli.clear_session_states", return_value=4),
                patch("agent_voice.cli.truncate_pipeline_log"),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "clear", "--all", "--yes"])
            clear_events.assert_called_once()
        self.assertIn("Cleared 1 event", out.getvalue())


class AutostartCommandTests(unittest.TestCase):
    def test_enable_sets_managed_and_prints_labels(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli.sys.platform", "darwin"),
                patch("agent_voice.cli.enable_autostart", return_value=["com.voiccce.daemon"]) as enable,
                patch("agent_voice.cli.set_autostart_managed") as set_managed,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "autostart", "enable"])
            enable.assert_called_once()
            self.assertIs(set_managed.call_args.args[1], True)
        self.assertIn("com.voiccce.daemon", out.getvalue())

    def test_enable_non_darwin_warns(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli.sys.platform", "linux"),
                patch("agent_voice.cli.enable_autostart") as enable,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "autostart", "enable"])
            enable.assert_not_called()
        self.assertIn("launchd", out.getvalue())

    def test_disable_sets_managed_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli.sys.platform", "darwin"),
                patch("agent_voice.cli.disable_autostart", return_value=["com.voiccce.daemon"]),
                patch("agent_voice.cli.set_autostart_managed") as set_managed,
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "autostart", "disable"])
            self.assertIs(set_managed.call_args.args[1], False)

    def test_status_reports_managed_flag(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            status = {
                "com.voiccce.daemon": {"plist_present": True, "loaded": True},
                "com.voiccce.menubar": {"plist_present": False, "loaded": False},
            }
            with (
                patch("agent_voice.cli.sys.platform", "darwin"),
                patch("agent_voice.cli.autostart_status", return_value=status),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "autostart", "status"])
        self.assertIn("Autostart managed:", out.getvalue())
        self.assertIn("loaded", out.getvalue())


class UninstallCommandTests(unittest.TestCase):
    def test_uninstall_target_removes_only_that_integration(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            result = SimpleNamespace(
                removed_events=("Stop",),
                settings_path=Path("/x/settings.json"),
                wrapper_removed=True,
            )
            with (
                patch("agent_voice.cli.remove_claude_code_personal", return_value=result) as remove,
                patch("agent_voice.cli.run_teardown") as teardown,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "uninstall", "claude-code"])
            remove.assert_called_once()
            teardown.assert_not_called()
        self.assertIn("Removed claude-code hooks: Stop", out.getvalue())

    def test_uninstall_no_target_runs_full_teardown(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            report = SimpleNamespace(
                stopped=["daemon"],
                removed_hooks={"claude-code": ["Stop"], "pi": True},
                removed_wrappers=["/x/wrapper"],
                removed_autostart=["com.voiccce.daemon"],
                keychain_deleted=True,
                backups_restored=[],
                data_removed=False,
                notes=["Kept data directory."],
                package_command=["pipx", "uninstall", "voiccce"],
            )
            with (
                patch("agent_voice.cli.detect_wired_integrations", return_value=["claude-code"]),
                patch("agent_voice.cli.run_teardown", return_value=report) as teardown,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "uninstall", "--yes"])
            teardown.assert_called_once()
            plan = teardown.call_args.args[1]
            self.assertFalse(plan.purge_data)
        output = out.getvalue()
        self.assertIn("pipx uninstall voiccce", output)
        self.assertIn("Keychain secret deleted: True", output)

    def test_uninstall_purge_passes_purge_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            report = SimpleNamespace(
                stopped=[], removed_hooks={}, removed_wrappers=[], removed_autostart=[],
                keychain_deleted=False, backups_restored=[], data_removed=True,
                notes=[], package_command=["pip", "uninstall", "-y", "voiccce"],
            )
            with (
                patch("agent_voice.cli.detect_wired_integrations", return_value=[]),
                patch("agent_voice.cli.run_teardown", return_value=report) as teardown,
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "uninstall", "--purge", "--yes"])
            self.assertTrue(teardown.call_args.args[1].purge_data)

    def test_uninstall_aborts_when_confirm_declined(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli._interactive", return_value=True),
                patch("agent_voice.cli.confirm", return_value=False),
                patch("agent_voice.cli.detect_wired_integrations", return_value=[]),
                patch("agent_voice.cli.run_teardown") as teardown,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "uninstall"])
            teardown.assert_not_called()
        self.assertIn("Aborted", out.getvalue())

    def test_uninstall_no_target_non_tty_without_yes_refuses(self) -> None:
        # Full teardown is irreversible: a non-interactive run without --yes must
        # refuse and exit non-zero, never tear down unprompted.
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli._interactive", return_value=False),
                patch("agent_voice.cli.detect_wired_integrations", return_value=[]),
                patch("agent_voice.cli.run_teardown") as teardown,
                redirect_stdout(out),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(["--config", str(config_path), "uninstall"])
            self.assertEqual(raised.exception.code, 2)
            teardown.assert_not_called()
        self.assertIn("Refusing", out.getvalue())
        self.assertIn("--yes", out.getvalue())

    def test_uninstall_no_target_non_tty_with_yes_proceeds(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            report = SimpleNamespace(
                stopped=[], removed_hooks={}, removed_wrappers=[], removed_autostart=[],
                keychain_deleted=False, backups_restored=[], data_removed=False,
                notes=[], package_command=["pipx", "uninstall", "voiccce"],
            )
            with (
                patch("agent_voice.cli._interactive", return_value=False),
                patch("agent_voice.cli.detect_wired_integrations", return_value=[]),
                patch("agent_voice.cli.run_teardown", return_value=report) as teardown,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "uninstall", "--yes"])
            teardown.assert_called_once()
        self.assertIn("pipx uninstall voiccce", out.getvalue())

    def test_uninstall_purge_non_tty_without_yes_refuses(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli._interactive", return_value=False),
                patch("agent_voice.cli.detect_wired_integrations", return_value=[]),
                patch("agent_voice.cli.run_teardown") as teardown,
                redirect_stdout(out),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(["--config", str(config_path), "uninstall", "--purge"])
            self.assertEqual(raised.exception.code, 2)
            teardown.assert_not_called()
        self.assertIn("Refusing", out.getvalue())

    def test_uninstall_target_non_tty_proceeds_without_yes(self) -> None:
        # Single-target hook removal is reversible (re-setup re-wires it), so it
        # stays non-destructive and needs no --yes even non-interactively.
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            result = SimpleNamespace(
                removed_events=("Stop",),
                settings_path=Path("/x/settings.json"),
                wrapper_removed=True,
            )
            with (
                patch("agent_voice.cli._interactive", return_value=False),
                patch("agent_voice.cli.remove_claude_code_personal", return_value=result) as remove,
                patch("agent_voice.cli.run_teardown") as teardown,
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "uninstall", "claude-code"])
            remove.assert_called_once()
            teardown.assert_not_called()
        self.assertIn("Removed claude-code hooks: Stop", out.getvalue())


class ConfigSettersTests(unittest.TestCase):
    def _run_config(self, config_path: Path, *args: str) -> str:
        out = StringIO()
        with redirect_stdout(out):
            main(["--config", str(config_path), "config", *args])
        return out.getvalue()

    def test_summary_privacy_and_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            self._run_config(config_path, "--summary", "off", "--summary-privacy", "metadata_only")
            config = load_config(config_path)
            self.assertFalse(config.summary_enabled)
            self.assertEqual(config.summary_privacy_level, "metadata_only")

    def test_summary_model_provider_pipeline_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            self._run_config(
                config_path,
                "--summary-model", "gpt-5.4-mini",
                "--summary-provider", "fallback",
                "--summary-pipeline-log", "off",
            )
            config = load_config(config_path)
            self.assertEqual(config.summary_model, "gpt-5.4-mini")
            self.assertEqual(config.summary_provider, "fallback")
            self.assertFalse(config.summary_pipeline_log)

    def test_event_flag_toggles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            self._run_config(config_path, "--event", "subagent_finished=on", "--event", "task_failed=off")
            config = load_config(config_path)
            self.assertTrue(config.notify_subagent_finished)
            self.assertFalse(config.notify_task_failed)

    def test_unknown_event_name_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with redirect_stdout(StringIO()), self.assertRaises(SystemExit):
                main(["--config", str(config_path), "config", "--event", "bogus=on"])

    def test_spend_caps_and_rate_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            output = self._run_config(
                config_path,
                "--max-events-per-minute", "12",
                "--daily-spend-cap", "1.5",
                "--monthly-spend-cap", "20",
                "--event-retention-days", "7",
            )
            config = load_config(config_path)
            self.assertEqual(config.max_events_per_minute, 12)
            self.assertEqual(config.daily_spend_cap_usd, 1.5)
            self.assertEqual(config.monthly_spend_cap_usd, 20.0)
            self.assertEqual(config.event_retention_days, 7)
            self.assertIn("Max events per minute: 12", output)
            self.assertIn("Event retention days: 7", output)

    def test_interrupt_on_reply_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            self._run_config(config_path, "--interrupt-on-reply", "off")
            self.assertFalse(load_config(config_path).voice_interrupt_on_user_input)

    def test_reset_section_writes_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            self._run_config(config_path, "--summary", "off")  # diverge from defaults
            output = self._run_config(config_path, "--reset", "--reset-section", "summary")
            self.assertIn("Backup:", output)
            self.assertTrue(load_config(config_path).summary_enabled)

    def test_reset_unknown_section_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with redirect_stdout(StringIO()), self.assertRaises(SystemExit):
                main(["--config", str(config_path), "config", "--reset", "--reset-section", "nope"])

    def test_config_display_shows_new_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            output = self._run_config(config_path)
            self.assertIn("Quiet hours:", output)
            self.assertIn("Summary pipeline log:", output)
            self.assertIn("Interrupt on reply:", output)


class TestCommandFeedbackTests(unittest.TestCase):
    def _router(self, results):
        router = MagicMock()
        router.deliver.return_value = results
        return router

    def test_success_reports_channel(self) -> None:
        out = StringIO()
        results = [SimpleNamespace(delivered=True, spoken=True, channel="macos_say", error=None)]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with (
                patch("agent_voice.cli.DeliveryRouter", return_value=self._router(results)),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "test"])
        self.assertIn("Test played via macos_say", out.getvalue())

    def test_failure_reports_error_and_hint_and_exits(self) -> None:
        out = StringIO()
        results = [SimpleNamespace(delivered=False, spoken=False, channel="openai_tts", error="HTTP 401: invalid key")]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with (
                patch("agent_voice.cli.DeliveryRouter", return_value=self._router(results)),
                redirect_stdout(out),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(["--config", str(config_path), "test"])
        self.assertEqual(raised.exception.code, 1)
        output = out.getvalue()
        self.assertIn("Test failed", output)
        self.assertIn("voiccce secret set openai", output)

    def test_remediation_hint_mapping(self) -> None:
        self.assertIn("unmute", _test_remediation_hint("voice muted"))
        self.assertIn("secret set openai", _test_remediation_hint("HTTP 401: nope"))
        self.assertIn("afplay", _test_remediation_hint("afplay command not found"))
        self.assertIsNone(_test_remediation_hint(None))


class NonMacosWarningTests(unittest.TestCase):
    def test_install_warns_on_non_macos(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with (
                patch("agent_voice.cli.sys.platform", "linux"),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "install"])
        self.assertIn("targets macOS", out.getvalue())

    def test_setup_warns_on_non_macos_and_prints_privacy_note(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            router = MagicMock()
            router.deliver.return_value = [SimpleNamespace(spoken=True, error=None)]
            with (
                patch("agent_voice.cli.sys.platform", "linux"),
                patch("agent_voice.cli._maybe_setup_menubar"),
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                patch(
                    "agent_voice.cli.validate_openai_tts_key",
                    return_value=SimpleNamespace(ok=True, error=None),
                ),
                patch("agent_voice.cli.install_claude_code_personal",
                      side_effect=lambda **kw: SimpleNamespace(settings_path=Path("/tmp/s.json"))),
                patch("agent_voice.cli.start_daemon", return_value=1),
                patch("agent_voice.cli.DeliveryRouter", return_value=router),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "setup", "claude-code", "--no-test"])
        output = out.getvalue()
        self.assertIn("targets macOS", output)
        self.assertIn("summary-privacy metadata_only", output)


class FormatBytesTests(unittest.TestCase):
    def test_format_bytes(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(5 * 1024 * 1024), "5.0 MB")


class StatusCommandTests(unittest.TestCase):
    def test_status_shows_version_and_agent_wiring(self) -> None:
        out = StringIO()
        wiring = [
            SimpleNamespace(agent="claude-code", wired=True, events=("Stop",), detail="wired"),
            SimpleNamespace(agent="pi", wired=False, events=(), detail="no extension"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli._resolve_version", return_value="7.7.7"),
                patch("agent_voice.cli.inspect_agent_wiring", return_value=wiring),
                patch("agent_voice.cli.in_quiet_hours", return_value=False),
                patch("agent_voice.cli.stale_pid_warnings", return_value=[]),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "status"])
        output = out.getvalue()
        self.assertIn("Version: 7.7.7", output)
        self.assertIn("claude-code: wired (Stop)", output)
        self.assertIn("pi: not wired", output)
        self.assertNotIn("Adapters:", output)

    def test_status_reports_quiet_hours_and_stale_pid(self) -> None:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            with (
                patch("agent_voice.cli.inspect_agent_wiring", return_value=[]),
                patch("agent_voice.cli.in_quiet_hours", return_value=True),
                patch("agent_voice.cli.stale_pid_warnings", return_value=[("daemon", 999)]),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "status"])
        output = out.getvalue()
        self.assertIn("Quiet hours: active", output)
        self.assertIn("Stale pid: daemon pid 999", output)

    def test_status_always_shows_quiet_hours_window_when_inactive(self) -> None:
        # H5: the night-silence default is enforced, so the window + enabled
        # state must be discoverable even when not currently active.
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            config = load_config(config_path)
            with (
                patch("agent_voice.cli.inspect_agent_wiring", return_value=[]),
                patch("agent_voice.cli.in_quiet_hours", return_value=False),
                patch("agent_voice.cli.stale_pid_warnings", return_value=[]),
                redirect_stdout(out),
            ):
                main(["--config", str(config_path), "status"])
        output = out.getvalue()
        self.assertIn("Quiet hours: enabled", output)
        self.assertIn(f"{config.quiet_hours_from}-{config.quiet_hours_to}", output)


if __name__ == "__main__":
    unittest.main()
