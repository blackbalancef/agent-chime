import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_voice.installer import WrapperImportError
from agent_voice.installer import claude_code
from agent_voice.installer.claude_code import (
    MARKER,
    install_claude_code_personal,
    remove_claude_code_personal,
    restore_latest_backup,
    restore_original_backup,
)


class ClaudeInstallerTests(unittest.TestCase):
    def test_installer_preserves_existing_hooks_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "settings.json"
            config_path = root / "config.toml"
            wrapper_path = root / "bin" / "hook"
            settings_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "bash existing.sh",
                                        }
                                    ]
                                },
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "AGENT_CHIME=1 /old/agent-chime-claude-hook Stop",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            first = install_claude_code_personal(
                repo_root=Path.cwd(),
                settings_path=settings_path,
                config_path=config_path,
                wrapper_path=wrapper_path,
                python_executable=root / "venv" / "bin" / "python",
            )
            second = install_claude_code_personal(
                repo_root=Path.cwd(),
                settings_path=settings_path,
                config_path=config_path,
                wrapper_path=wrapper_path,
                python_executable=root / "venv" / "bin" / "python",
            )

            data = json.loads(settings_path.read_text(encoding="utf-8"))
            stop_entries = data["hooks"]["Stop"]
            commands = [
                hook["command"]
                for entry in stop_entries
                for hook in entry.get("hooks", [])
            ]
            self.assertIn("bash existing.sh", commands)
            self.assertFalse(any("AGENT_CHIME=1" in command for command in commands))
            self.assertEqual(sum(MARKER in command for command in commands), 1)
            self.assertTrue(first.backup_path.exists())
            self.assertTrue(second.backup_path.exists())
            self.assertTrue(wrapper_path.exists())
            self.assertTrue(config_path.exists())
            wrapper = wrapper_path.read_text(encoding="utf-8")
            python_executable = (root / "venv" / "bin" / "python").resolve()
            self.assertIn(f"PYTHON_BIN={python_executable}", wrapper)
            self.assertNotIn("/usr/bin/env python3 -m agent_voice", wrapper)

    def test_verify_raises_when_interpreter_cannot_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(WrapperImportError):
                install_claude_code_personal(
                    repo_root=root,  # empty dir → agent_voice not importable
                    settings_path=root / "settings.json",
                    config_path=root / "config.toml",
                    wrapper_path=root / "bin" / "hook",
                    python_executable=sys.executable,
                    verify=True,
                )

    def test_verify_passes_for_valid_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = install_claude_code_personal(
                repo_root=Path.cwd(),  # repo root has the agent_voice package
                settings_path=root / "settings.json",
                config_path=root / "config.toml",
                wrapper_path=root / "bin" / "hook",
                python_executable=sys.executable,
                verify=True,
            )
            self.assertTrue(result.wrapper_path.exists())


class ClaudeRemoveTests(unittest.TestCase):
    def test_remove_strips_voiccce_but_keeps_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "settings.json"
            config_path = root / "config.toml"
            wrapper_path = root / "bin" / "hook"
            settings_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": "bash existing.sh"}
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            install_claude_code_personal(
                repo_root=Path.cwd(),
                settings_path=settings_path,
                config_path=config_path,
                wrapper_path=wrapper_path,
                python_executable=root / "venv" / "bin" / "python",
            )
            self.assertTrue(wrapper_path.exists())

            result = remove_claude_code_personal(
                settings_path=settings_path,
                wrapper_path=wrapper_path,
            )

            data = json.loads(settings_path.read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for entries in data["hooks"].values()
                for entry in entries
                for hook in entry.get("hooks", [])
            ]
            self.assertIn("bash existing.sh", commands)
            self.assertFalse(any(MARKER in command for command in commands))
            self.assertIn("Stop", result.removed_events)
            self.assertTrue(result.wrapper_removed)
            self.assertFalse(wrapper_path.exists())
            self.assertIsNotNone(result.backup_path)
            self.assertTrue(result.backup_path.exists())

    def test_remove_when_absent_is_safe_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": "bash existing.sh"}
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            before = settings_path.read_text(encoding="utf-8")

            result = remove_claude_code_personal(
                settings_path=settings_path,
                wrapper_path=root / "bin" / "missing-hook",
            )

            self.assertEqual(result.removed_events, ())
            self.assertIsNone(result.backup_path)
            self.assertFalse(result.wrapper_removed)
            self.assertEqual(settings_path.read_text(encoding="utf-8"), before)

    def test_remove_when_no_settings_file_is_safe_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "settings.json"
            result = remove_claude_code_personal(
                settings_path=settings_path,
                wrapper_path=root / "bin" / "missing-hook",
            )
            self.assertEqual(result.removed_events, ())
            self.assertIsNone(result.backup_path)
            self.assertFalse(settings_path.exists())

    def test_restore_latest_backup_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "settings.json"
            config_path = root / "config.toml"
            wrapper_path = root / "bin" / "hook"
            original = {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "bash existing.sh"}]}
                    ]
                }
            }
            settings_path.write_text(json.dumps(original), encoding="utf-8")

            install_claude_code_personal(
                repo_root=Path.cwd(),
                settings_path=settings_path,
                config_path=config_path,
                wrapper_path=wrapper_path,
                python_executable=root / "venv" / "bin" / "python",
            )
            installed = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertTrue(
                any(
                    MARKER in hook["command"]
                    for entries in installed["hooks"].values()
                    for entry in entries
                    for hook in entry.get("hooks", [])
                )
            )

            restored = restore_latest_backup(settings_path)
            self.assertIsNotNone(restored)
            self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8")), original)

    def test_restore_when_no_backup_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            self.assertIsNone(restore_latest_backup(settings_path))

    def test_restore_original_backup_after_install_then_remove_yields_preinstall(self) -> None:
        """H3 regression: install -> remove -> restore must yield pre-install state.

        ``remove_claude_code_personal`` takes a fresh backup of the still-installed
        state, so the *latest* backup is the Voiccce-installed one. Restoring it
        re-applies our hooks. ``restore_original_backup`` restores the OLDEST
        (pre-install) backup instead, giving the user back exactly what they had.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = root / "settings.json"
            config_path = root / "config.toml"
            wrapper_path = root / "bin" / "hook"
            original = {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "bash existing.sh"}]}
                    ]
                }
            }
            settings_path.write_text(json.dumps(original), encoding="utf-8")
            preinstall = settings_path.read_text(encoding="utf-8")

            install_claude_code_personal(
                repo_root=Path.cwd(),
                settings_path=settings_path,
                config_path=config_path,
                wrapper_path=wrapper_path,
                python_executable=root / "venv" / "bin" / "python",
            )
            remove_claude_code_personal(settings_path=settings_path, wrapper_path=wrapper_path)

            # The newest backup captures the still-installed Voiccce state, so
            # restoring it would wrongly re-apply our hooks.
            latest = restore_latest_backup(settings_path)
            self.assertIsNotNone(latest)
            reapplied = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertTrue(
                any(
                    MARKER in hook["command"]
                    for entries in reapplied["hooks"].values()
                    for entry in entries
                    for hook in entry.get("hooks", [])
                ),
                "latest backup re-applies Voiccce (this is exactly the H3 bug)",
            )

            # Restoring the ORIGINAL (oldest) backup gives back the user's file.
            restored = restore_original_backup(settings_path)
            self.assertIsNotNone(restored)
            self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8")), original)
            self.assertEqual(settings_path.read_text(encoding="utf-8").rstrip("\n"), preinstall.rstrip("\n"))

    def test_two_backups_in_same_second_do_not_collide(self) -> None:
        """M4 regression: a fast install+remove must keep BOTH backups.

        A second-resolution stamp made two backups taken in the same second share
        a filename, so the second overwrote the first and the pre-install backup
        was lost. Pin the clock to a single second and confirm two distinct files.
        """
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text('{"hooks": {}}', encoding="utf-8")

            frozen = datetime(2026, 6, 25, 12, 0, 0, 0, tzinfo=UTC)

            class _FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):  # noqa: D401 - mimics datetime.now signature
                    return frozen

            with mock.patch.object(claude_code, "datetime", _FrozenDateTime):
                first = claude_code._backup_settings(settings_path)
                second = claude_code._backup_settings(settings_path)

            self.assertNotEqual(first, second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            backups = list(settings_path.parent.glob(f"{settings_path.name}.voiccce-backup.*"))
            self.assertEqual(len(backups), 2)


if __name__ == "__main__":
    unittest.main()
