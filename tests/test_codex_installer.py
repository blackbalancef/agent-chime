import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_voice.installer import codex
from agent_voice.installer.codex import (
    MARKER,
    install_codex_personal,
    remove_codex_personal,
    restore_latest_backup,
    restore_original_backup,
)


class CodexInstallerTests(unittest.TestCase):
    def test_installer_preserves_existing_hooks_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "codex" / "hooks.json"
            config_path = root / "config.toml"
            wrapper_path = root / "bin" / "hook"
            hooks_path.parent.mkdir(parents=True)
            hooks_path.write_text(
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
                                            "command": "/usr/bin/env AGENT_CHIME=1 /old/agent-chime-codex-hook Stop",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            first = install_codex_personal(
                repo_root=Path.cwd(),
                hooks_path=hooks_path,
                config_path=config_path,
                wrapper_path=wrapper_path,
                python_executable=root / "venv" / "bin" / "python",
            )
            second = install_codex_personal(
                repo_root=Path.cwd(),
                hooks_path=hooks_path,
                config_path=config_path,
                wrapper_path=wrapper_path,
                python_executable=root / "venv" / "bin" / "python",
            )

            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            stop_entries = data["hooks"]["Stop"]
            commands = [
                hook["command"]
                for entry in stop_entries
                for hook in entry.get("hooks", [])
            ]
            self.assertIn("bash existing.sh", commands)
            self.assertFalse(any("AGENT_CHIME=1" in command for command in commands))
            self.assertEqual(sum(MARKER in command for command in commands), 1)
            agent_chime_command = next(command for command in commands if MARKER in command)
            self.assertTrue(agent_chime_command.startswith("/usr/bin/env "))
            self.assertIn("PermissionRequest", data["hooks"])
            self.assertIn("SubagentStop", data["hooks"])
            self.assertTrue(first.backup_path.exists())
            self.assertTrue(second.backup_path.exists())
            self.assertTrue(wrapper_path.exists())
            self.assertTrue(config_path.exists())
            wrapper = wrapper_path.read_text(encoding="utf-8")
            python_executable = (root / "venv" / "bin" / "python").resolve()
            self.assertIn(f"PYTHON_BIN={python_executable}", wrapper)
            self.assertIn("collect codex", wrapper)


class CodexRemoveTests(unittest.TestCase):
    def test_remove_strips_voiccce_but_keeps_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "codex" / "hooks.json"
            config_path = root / "config.toml"
            wrapper_path = root / "bin" / "hook"
            hooks_path.parent.mkdir(parents=True)
            hooks_path.write_text(
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

            install_codex_personal(
                repo_root=Path.cwd(),
                hooks_path=hooks_path,
                config_path=config_path,
                wrapper_path=wrapper_path,
                python_executable=root / "venv" / "bin" / "python",
            )
            self.assertTrue(wrapper_path.exists())

            result = remove_codex_personal(hooks_path=hooks_path, wrapper_path=wrapper_path)

            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for entries in data["hooks"].values()
                for entry in entries
                for hook in entry.get("hooks", [])
            ]
            self.assertIn("bash existing.sh", commands)
            self.assertFalse(any(MARKER in command for command in commands))
            self.assertIn("Stop", result.removed_events)
            self.assertNotIn("PermissionRequest", data["hooks"])
            self.assertTrue(result.wrapper_removed)
            self.assertFalse(wrapper_path.exists())
            self.assertIsNotNone(result.backup_path)
            self.assertTrue(result.backup_path.exists())

    def test_remove_when_absent_is_safe_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "codex" / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            hooks_path.write_text(
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
            before = hooks_path.read_text(encoding="utf-8")

            result = remove_codex_personal(
                hooks_path=hooks_path,
                wrapper_path=root / "bin" / "missing-hook",
            )

            self.assertEqual(result.removed_events, ())
            self.assertIsNone(result.backup_path)
            self.assertFalse(result.wrapper_removed)
            self.assertEqual(hooks_path.read_text(encoding="utf-8"), before)

    def test_remove_when_no_hooks_file_is_safe_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "codex" / "hooks.json"
            result = remove_codex_personal(
                hooks_path=hooks_path,
                wrapper_path=root / "bin" / "missing-hook",
            )
            self.assertEqual(result.removed_events, ())
            self.assertIsNone(result.backup_path)
            self.assertFalse(hooks_path.exists())

    def test_restore_latest_backup_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "codex" / "hooks.json"
            config_path = root / "config.toml"
            wrapper_path = root / "bin" / "hook"
            hooks_path.parent.mkdir(parents=True)
            original = {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "bash existing.sh"}]}
                    ]
                }
            }
            hooks_path.write_text(json.dumps(original), encoding="utf-8")

            install_codex_personal(
                repo_root=Path.cwd(),
                hooks_path=hooks_path,
                config_path=config_path,
                wrapper_path=wrapper_path,
                python_executable=root / "venv" / "bin" / "python",
            )

            restored = restore_latest_backup(hooks_path=hooks_path)
            self.assertIsNotNone(restored)
            self.assertEqual(json.loads(hooks_path.read_text(encoding="utf-8")), original)

    def test_restore_when_no_backup_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "codex" / "hooks.json"
            self.assertIsNone(restore_latest_backup(hooks_path=hooks_path))

    def test_restore_original_backup_after_install_then_remove_yields_preinstall(self) -> None:
        """H3 regression: install -> remove -> restore must yield pre-install state.

        ``remove_codex_personal`` takes a fresh backup of the still-installed
        state, so the *latest* backup is the Voiccce-installed one. Restoring it
        re-applies our hooks. ``restore_original_backup`` restores the OLDEST
        (pre-install) backup instead, giving the user back exactly what they had.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks_path = root / "codex" / "hooks.json"
            config_path = root / "config.toml"
            wrapper_path = root / "bin" / "hook"
            hooks_path.parent.mkdir(parents=True)
            original = {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "bash existing.sh"}]}
                    ]
                }
            }
            hooks_path.write_text(json.dumps(original), encoding="utf-8")

            install_codex_personal(
                repo_root=Path.cwd(),
                hooks_path=hooks_path,
                config_path=config_path,
                wrapper_path=wrapper_path,
                python_executable=root / "venv" / "bin" / "python",
            )
            remove_codex_personal(hooks_path=hooks_path, wrapper_path=wrapper_path)

            # The newest backup captures the still-installed Voiccce state, so
            # restoring it would wrongly re-apply our hooks.
            latest = restore_latest_backup(hooks_path=hooks_path)
            self.assertIsNotNone(latest)
            reapplied = json.loads(hooks_path.read_text(encoding="utf-8"))
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
            restored = restore_original_backup(hooks_path=hooks_path)
            self.assertIsNotNone(restored)
            self.assertEqual(json.loads(hooks_path.read_text(encoding="utf-8")), original)

    def test_two_backups_in_same_second_do_not_collide(self) -> None:
        """M4 regression: a fast install+remove must keep BOTH backups.

        A second-resolution stamp made two backups taken in the same second share
        a filename, so the second overwrote the first and the pre-install backup
        was lost. Pin the clock to a single second and confirm two distinct files.
        """
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = Path(tmp) / "codex" / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            hooks_path.write_text('{"hooks": {}}', encoding="utf-8")

            frozen = datetime(2026, 6, 25, 12, 0, 0, 0, tzinfo=UTC)

            class _FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):  # noqa: D401 - mimics datetime.now signature
                    return frozen

            with mock.patch.object(codex, "datetime", _FrozenDateTime):
                first = codex._backup_hooks(hooks_path)
                second = codex._backup_hooks(hooks_path)

            self.assertNotEqual(first, second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            backups = list(hooks_path.parent.glob(f"{hooks_path.name}.voiccce-backup.*"))
            self.assertEqual(len(backups), 2)


if __name__ == "__main__":
    unittest.main()
