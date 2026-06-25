import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_voice import doctor
from agent_voice.config import AgentVoiceConfig
from agent_voice.doctor import (
    AgentWiring,
    CheckResult,
    check_config,
    check_database,
    check_failed_events,
    check_hooks_wired,
    check_mute,
    check_openai_key,
    check_required_tools,
    check_wrapper_import,
    doctor_ok,
    inspect_agent_wiring,
    run_doctor,
)
from agent_voice.installer import claude_code, codex, pi
from agent_voice.secrets import OpenAIKeyValidation, SecretStatus


def _config(tmp: str, **overrides) -> AgentVoiceConfig:
    base = dict(
        config_path=Path(tmp) / "config.toml",
        database_path=Path(tmp) / "events.sqlite3",
    )
    base.update(overrides)
    return AgentVoiceConfig(**base)


def _write_claude_settings(path: Path, *, wired: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if wired:
        command = f"{claude_code.MARKER} /tmp/voiccce-claude-hook Stop"
    else:
        command = "/usr/bin/true"
    settings = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": command}]}],
        }
    }
    path.write_text(json.dumps(settings), encoding="utf-8")


class InspectAgentWiringTests(unittest.TestCase):
    def test_detects_wired_claude_from_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            settings_path = Path(tmp) / "settings.json"
            _write_claude_settings(settings_path, wired=True)
            codex_home = Path(tmp) / "codex"
            pi_agent = Path(tmp) / "pi" / "agent"
            with mock.patch.object(claude_code, "PERSONAL_SETTINGS_PATH", settings_path), \
                mock.patch.object(codex, "_default_codex_home", return_value=codex_home), \
                mock.patch.object(pi, "_resolve_agent_dir", return_value=pi_agent):
                wirings = inspect_agent_wiring(config)
            by_agent = {w.agent: w for w in wirings}
            self.assertTrue(by_agent["claude-code"].wired)
            self.assertIn("Stop", by_agent["claude-code"].events)
            # No codex/pi files present -> not wired, handled gracefully.
            self.assertFalse(by_agent["codex"].wired)
            self.assertFalse(by_agent["pi"].wired)

    def test_unwired_claude_when_marker_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            settings_path = Path(tmp) / "settings.json"
            _write_claude_settings(settings_path, wired=False)
            codex_home = Path(tmp) / "codex"
            pi_agent = Path(tmp) / "pi" / "agent"
            with mock.patch.object(claude_code, "PERSONAL_SETTINGS_PATH", settings_path), \
                mock.patch.object(codex, "_default_codex_home", return_value=codex_home), \
                mock.patch.object(pi, "_resolve_agent_dir", return_value=pi_agent):
                wirings = inspect_agent_wiring(config)
            by_agent = {w.agent: w for w in wirings}
            self.assertFalse(by_agent["claude-code"].wired)
            self.assertEqual(by_agent["claude-code"].events, ())

    def test_missing_files_report_unwired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            settings_path = Path(tmp) / "missing.json"
            codex_home = Path(tmp) / "codex"
            pi_agent = Path(tmp) / "pi" / "agent"
            with mock.patch.object(claude_code, "PERSONAL_SETTINGS_PATH", settings_path), \
                mock.patch.object(codex, "_default_codex_home", return_value=codex_home), \
                mock.patch.object(pi, "_resolve_agent_dir", return_value=pi_agent):
                wirings = inspect_agent_wiring(config)
            self.assertTrue(all(not w.wired for w in wirings))
            self.assertEqual(len(wirings), 3)

    def test_detects_wired_codex_and_pi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            settings_path = Path(tmp) / "missing.json"
            codex_home = Path(tmp) / "codex"
            hooks_path = codex_home / "hooks.json"
            hooks_path.parent.mkdir(parents=True, exist_ok=True)
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": f"/usr/bin/env {codex.MARKER} /tmp/voiccce-codex-hook Stop",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            pi_agent = Path(tmp) / "pi" / "agent"
            ext_path = pi_agent / "extensions" / "voiccce.ts"
            ext_path.parent.mkdir(parents=True, exist_ok=True)
            ext_path.write_text(
                f"// {pi.MARKER} Voiccce\n"
                'pi.on("before_agent_start", () => {});  // UserPromptSubmit\n'
                'pi.on("agent_end", () => {});  // Stop\n',
                encoding="utf-8",
            )
            with mock.patch.object(claude_code, "PERSONAL_SETTINGS_PATH", settings_path), \
                mock.patch.object(codex, "_default_codex_home", return_value=codex_home), \
                mock.patch.object(pi, "_resolve_agent_dir", return_value=pi_agent):
                wirings = inspect_agent_wiring(config)
            by_agent = {w.agent: w for w in wirings}
            self.assertTrue(by_agent["codex"].wired)
            self.assertIn("Stop", by_agent["codex"].events)
            self.assertTrue(by_agent["pi"].wired)
            self.assertIn("Stop", by_agent["pi"].events)
            self.assertIn("UserPromptSubmit", by_agent["pi"].events)


class CheckHooksWiredTests(unittest.TestCase):
    def test_passes_when_any_wired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            wirings = [
                AgentWiring("claude-code", True, ("Stop",), "wired"),
                AgentWiring("codex", False, (), "no hooks"),
                AgentWiring("pi", False, (), "no ext"),
            ]
            with mock.patch.object(doctor, "inspect_agent_wiring", return_value=wirings):
                result = check_hooks_wired(config)
            self.assertTrue(result.ok)
            self.assertIn("claude-code", result.detail)

    def test_fails_when_none_wired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            wirings = [
                AgentWiring("claude-code", False, (), "no hooks"),
                AgentWiring("codex", False, (), "no hooks"),
                AgentWiring("pi", False, (), "no ext"),
            ]
            with mock.patch.object(doctor, "inspect_agent_wiring", return_value=wirings):
                result = check_hooks_wired(config)
            self.assertFalse(result.ok)
            self.assertTrue(result.hint)


class CheckConfigTests(unittest.TestCase):
    def test_passes_on_valid_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("[user]\nlanguage = \"en\"\n", encoding="utf-8")
            result = check_config(config_path)
            self.assertTrue(result.ok)

    def test_fails_on_malformed_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("this is = = not valid toml [[[", encoding="utf-8")
            result = check_config(config_path)
            self.assertFalse(result.ok)
            self.assertTrue(result.hint)

    def test_passes_when_config_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A missing config file is valid: load_config returns defaults.
            result = check_config(Path(tmp) / "does-not-exist.toml")
            self.assertTrue(result.ok)


class CheckDatabaseTests(unittest.TestCase):
    def test_opens_clean_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            result = check_database(config)
            self.assertTrue(result.ok)
            self.assertTrue(config.database_path.exists())

    def test_fails_on_corrupt_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            config.database_path.write_bytes(b"this is not a sqlite database file at all")
            result = check_database(config)
            self.assertFalse(result.ok)
            self.assertTrue(result.hint)


class CheckRequiredToolsTests(unittest.TestCase):
    def test_passes_when_all_present(self) -> None:
        with mock.patch("agent_voice.doctor.shutil.which", return_value="/usr/bin/tool"):
            result = check_required_tools()
        self.assertTrue(result.ok)

    def test_reports_missing_tools(self) -> None:
        def which(name: str) -> str | None:
            return None if name == "afplay" else f"/usr/bin/{name}"

        with mock.patch("agent_voice.doctor.shutil.which", side_effect=which):
            result = check_required_tools()
        self.assertFalse(result.ok)
        self.assertIn("afplay", result.detail)


class CheckOpenAIKeyTests(unittest.TestCase):
    def test_informational_pass_for_non_openai_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, voice_backend="macos_say")
            result = check_openai_key(config, validate=True)
            self.assertTrue(result.ok)

    def test_fails_when_key_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, voice_backend="openai_tts")
            missing = SecretStatus(source="missing", available=False)
            with mock.patch("agent_voice.doctor.secrets.get_openai_secret_status", return_value=missing):
                result = check_openai_key(config, validate=False)
            self.assertFalse(result.ok)
            self.assertTrue(result.hint)

    def test_validate_false_skips_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, voice_backend="openai_tts")
            present = SecretStatus(source="env", available=True)
            with mock.patch("agent_voice.doctor.secrets.get_openai_secret_status", return_value=present), \
                mock.patch("agent_voice.doctor.secrets.validate_openai_tts_key") as validator:
                result = check_openai_key(config, validate=False)
            validator.assert_not_called()
            self.assertTrue(result.ok)
            self.assertIn("skipped", result.detail)

    def test_validate_true_uses_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, voice_backend="openai_tts")
            present = SecretStatus(source="keychain", available=True)
            with mock.patch("agent_voice.doctor.secrets.get_openai_secret_status", return_value=present), \
                mock.patch("agent_voice.doctor.secrets.resolve_openai_api_key", return_value=("sk-test", present)), \
                mock.patch(
                    "agent_voice.doctor.secrets.validate_openai_tts_key",
                    return_value=OpenAIKeyValidation(ok=True),
                ) as validator:
                result = check_openai_key(config, validate=True)
            validator.assert_called_once()
            self.assertTrue(result.ok)

    def test_validate_true_reports_invalid_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, voice_backend="openai_tts")
            present = SecretStatus(source="env", available=True)
            with mock.patch("agent_voice.doctor.secrets.get_openai_secret_status", return_value=present), \
                mock.patch("agent_voice.doctor.secrets.resolve_openai_api_key", return_value=("sk-bad", present)), \
                mock.patch(
                    "agent_voice.doctor.secrets.validate_openai_tts_key",
                    return_value=OpenAIKeyValidation(ok=False, error="HTTP 401"),
                ):
                result = check_openai_key(config, validate=True)
            self.assertFalse(result.ok)
            self.assertIn("401", result.detail)


class CheckWrapperImportTests(unittest.TestCase):
    def test_skips_when_no_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            wrapper_path = Path(tmp) / "bin" / "voiccce-claude-hook"
            with mock.patch.object(claude_code, "WRAPPER_PATH", wrapper_path):
                result = check_wrapper_import(config)
            self.assertTrue(result.ok)
            self.assertIn("skipped", result.detail)

    def test_parses_wrapper_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            wrapper_path = Path(tmp) / "bin" / "voiccce-claude-hook"
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)
            wrapper_path.write_text(
                "#!/usr/bin/env bash\n"
                "REPO_ROOT='/repo/root'\n"
                "PYTHON_BIN='/usr/bin/python3'\n",
                encoding="utf-8",
            )
            with mock.patch.object(claude_code, "WRAPPER_PATH", wrapper_path), \
                mock.patch("agent_voice.doctor.verify_wrapper_imports") as verify:
                result = check_wrapper_import(config)
            verify.assert_called_once_with(Path("/usr/bin/python3"), Path("/repo/root"))
            self.assertTrue(result.ok)

    def test_reports_broken_interpreter(self) -> None:
        from agent_voice.installer import WrapperImportError

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            wrapper_path = Path(tmp) / "bin" / "voiccce-claude-hook"
            wrapper_path.parent.mkdir(parents=True, exist_ok=True)
            wrapper_path.write_text(
                "REPO_ROOT='/repo/root'\nPYTHON_BIN='/usr/bin/python3'\n",
                encoding="utf-8",
            )
            with mock.patch.object(claude_code, "WRAPPER_PATH", wrapper_path), \
                mock.patch(
                    "agent_voice.doctor.verify_wrapper_imports",
                    side_effect=WrapperImportError("cannot import agent_voice\nmore detail"),
                ):
                result = check_wrapper_import(config)
            self.assertFalse(result.ok)
            self.assertTrue(result.hint)


class CheckMuteTests(unittest.TestCase):
    def test_reports_not_muted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            result = check_mute(config)
            self.assertTrue(result.ok)
            self.assertIn("not muted", result.detail)


class CheckFailedEventsTests(unittest.TestCase):
    def test_no_failed_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            check_database(config)  # initialize the schema
            result = check_failed_events(config)
            self.assertTrue(result.ok)

    def test_counts_failed_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            check_database(config)
            conn = sqlite3.connect(config.database_path)
            try:
                conn.execute(
                    """
                    INSERT INTO events
                        (event_key, agent_name, event_type, raw_payload_json, status, created_at)
                    VALUES (?, ?, ?, ?, 'failed', ?)
                    """,
                    ("k1", "claude-code", "task_finished", "{}", 0),
                )
                conn.commit()
            finally:
                conn.close()
            result = check_failed_events(config)
            self.assertFalse(result.ok)
            self.assertIn("1", result.detail)
            self.assertTrue(result.hint)


class DoctorOkTests(unittest.TestCase):
    def test_true_when_all_ok(self) -> None:
        results = [
            CheckResult("a", True, "ok"),
            CheckResult("b", True, "ok"),
        ]
        self.assertTrue(doctor_ok(results))

    def test_false_when_any_fail(self) -> None:
        results = [
            CheckResult("a", True, "ok"),
            CheckResult("b", False, "bad"),
        ]
        self.assertFalse(doctor_ok(results))


class RunDoctorTests(unittest.TestCase):
    def test_runs_all_checks_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, voice_backend="macos_say")
            settings_path = Path(tmp) / "missing.json"
            codex_home = Path(tmp) / "codex"
            pi_agent = Path(tmp) / "pi" / "agent"
            wrapper_path = Path(tmp) / "bin" / "voiccce-claude-hook"
            with mock.patch.object(claude_code, "PERSONAL_SETTINGS_PATH", settings_path), \
                mock.patch.object(claude_code, "WRAPPER_PATH", wrapper_path), \
                mock.patch.object(codex, "_default_codex_home", return_value=codex_home), \
                mock.patch.object(pi, "_resolve_agent_dir", return_value=pi_agent), \
                mock.patch("agent_voice.doctor.shutil.which", return_value="/usr/bin/tool"):
                results = run_doctor(config, validate_key=False)
            names = {r.name for r in results}
            self.assertEqual(
                names,
                {
                    "config",
                    "database",
                    "hooks",
                    "wrapper",
                    "openai_key",
                    "tools",
                    "daemon",
                    "mute",
                    "failed_events",
                },
            )

    def test_validate_key_false_skips_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, voice_backend="openai_tts")
            settings_path = Path(tmp) / "missing.json"
            codex_home = Path(tmp) / "codex"
            pi_agent = Path(tmp) / "pi" / "agent"
            wrapper_path = Path(tmp) / "bin" / "voiccce-claude-hook"
            present = SecretStatus(source="env", available=True)
            with mock.patch.object(claude_code, "PERSONAL_SETTINGS_PATH", settings_path), \
                mock.patch.object(claude_code, "WRAPPER_PATH", wrapper_path), \
                mock.patch.object(codex, "_default_codex_home", return_value=codex_home), \
                mock.patch.object(pi, "_resolve_agent_dir", return_value=pi_agent), \
                mock.patch("agent_voice.doctor.shutil.which", return_value="/usr/bin/tool"), \
                mock.patch("agent_voice.doctor.secrets.get_openai_secret_status", return_value=present), \
                mock.patch("agent_voice.doctor.secrets.validate_openai_tts_key") as validator:
                results = run_doctor(config, validate_key=False)
            validator.assert_not_called()
            key_result = next(r for r in results if r.name == "openai_key")
            self.assertTrue(key_result.ok)


if __name__ == "__main__":
    unittest.main()
