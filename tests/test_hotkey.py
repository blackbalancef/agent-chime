import tempfile
import unittest
from pathlib import Path

from agent_voice.config import load_config, set_hotkey_config, write_default_config
from agent_voice.hotkey import (
    DEFAULT_STOP_SPEAKING_HOTKEY,
    HOTKEY_PRESETS,
    format_hotkey_display,
    parse_hotkey,
)

# Carbon modifier bit masks, mirrored from hotkey.py for assertion clarity.
_CONTROL, _OPTION, _SHIFT, _CMD = 0x1000, 0x0800, 0x0200, 0x0100


class ParseHotkeyTests(unittest.TestCase):
    def test_default_combo(self) -> None:
        parsed = parse_hotkey("alt+cmd+s")
        self.assertEqual(parsed.keycode, 0x01)  # kVK_ANSI_S
        self.assertEqual(parsed.carbon_modifiers, _OPTION | _CMD)
        self.assertEqual(parsed.canonical, "alt+cmd+s")
        self.assertEqual(parsed.display, "⌥⌘S")
        self.assertEqual(parsed.modifiers, ("alt", "cmd"))
        self.assertEqual(parsed.key, "s")

    def test_modifier_aliases_and_order_are_normalized(self) -> None:
        # Aliases, case, and token order all collapse to one canonical form.
        for spec in ("Command+Option+S", "S+cmd+opt", "⌘+⌥+s", "OPTION + COMMAND + s"):
            parsed = parse_hotkey(spec)
            self.assertEqual(parsed.canonical, "alt+cmd+s", spec)
            self.assertEqual(parsed.carbon_modifiers, _OPTION | _CMD, spec)

    def test_full_modifier_order_is_ctrl_alt_shift_cmd(self) -> None:
        parsed = parse_hotkey("cmd+shift+alt+ctrl+a")
        self.assertEqual(parsed.canonical, "ctrl+alt+shift+cmd+a")
        self.assertEqual(parsed.display, "⌃⌥⇧⌘A")
        self.assertEqual(parsed.carbon_modifiers, _CONTROL | _OPTION | _SHIFT | _CMD)

    def test_period_and_punctuation_keys(self) -> None:
        parsed = parse_hotkey("ctrl+alt+cmd+.")
        self.assertEqual(parsed.keycode, 0x2F)
        self.assertEqual(parsed.display, "⌃⌥⌘.")
        self.assertEqual(parse_hotkey("cmd+period").canonical, "cmd+.")

    def test_named_keys_and_function_keys(self) -> None:
        self.assertEqual(parse_hotkey("cmd+space").keycode, 0x31)
        self.assertEqual(parse_hotkey("cmd+space").display, "⌘Space")
        self.assertEqual(parse_hotkey("cmd+shift+f5").keycode, 0x60)
        self.assertEqual(parse_hotkey("cmd+escape").display, "⌘Esc")
        self.assertEqual(parse_hotkey("cmd+left").display, "⌘←")

    def test_duplicate_modifiers_collapse(self) -> None:
        parsed = parse_hotkey("cmd+cmd+s")
        self.assertEqual(parsed.canonical, "cmd+s")
        self.assertEqual(parsed.carbon_modifiers, _CMD)

    def test_canonical_round_trips(self) -> None:
        for spec in ("Command+Shift+P", "ctrl+alt+cmd+.", "opt+cmd+f12"):
            canonical = parse_hotkey(spec).canonical
            self.assertEqual(parse_hotkey(canonical).canonical, canonical)

    def test_all_presets_parse(self) -> None:
        for spec in HOTKEY_PRESETS:
            parsed = parse_hotkey(spec)
            self.assertEqual(parsed.canonical, spec)  # presets are already canonical
            self.assertTrue(parsed.modifiers)

    def test_default_is_a_valid_preset(self) -> None:
        self.assertIn(DEFAULT_STOP_SPEAKING_HOTKEY, HOTKEY_PRESETS)
        parse_hotkey(DEFAULT_STOP_SPEAKING_HOTKEY)  # must not raise


class ParseHotkeyErrorTests(unittest.TestCase):
    def test_empty(self) -> None:
        for spec in ("", "   ", "+", "++"):
            with self.assertRaises(ValueError):
                parse_hotkey(spec)

    def test_modifier_only(self) -> None:
        for spec in ("cmd", "cmd+shift", "ctrl+alt"):
            with self.assertRaises(ValueError):
                parse_hotkey(spec)

    def test_missing_modifier(self) -> None:
        # A bare key would steal that key from every app, so a modifier is required.
        for spec in ("s", "f5", "."):
            with self.assertRaises(ValueError):
                parse_hotkey(spec)

    def test_unknown_key(self) -> None:
        for spec in ("cmd+nope", "cmd+f99", "alt+cmd+øø"):
            with self.assertRaises(ValueError):
                parse_hotkey(spec)

    def test_multiple_keys(self) -> None:
        with self.assertRaises(ValueError):
            parse_hotkey("cmd+a+b")

    def test_non_string(self) -> None:
        with self.assertRaises(ValueError):
            parse_hotkey(None)  # type: ignore[arg-type]


class FormatHotkeyDisplayTests(unittest.TestCase):
    def test_valid_spec_renders_symbols(self) -> None:
        self.assertEqual(format_hotkey_display("alt+cmd+s"), "⌥⌘S")

    def test_invalid_spec_returns_input(self) -> None:
        # Defensive: never crash the menu/status output on a hand-edited bad value.
        self.assertEqual(format_hotkey_display("garbage"), "garbage")


class HotkeyConfigRoundTripTests(unittest.TestCase):
    def test_default_config_enables_default_hotkey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_default_config(config_path)
            config = load_config(config_path)
            self.assertTrue(config.hotkey_enabled)
            self.assertEqual(config.hotkey_stop_speaking, DEFAULT_STOP_SPEAKING_HOTKEY)

    def test_set_hotkey_config_stores_canonical_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_hotkey_config(config_path, enabled=True, stop_speaking="Command+Option+S")
            config = load_config(config_path)
            self.assertTrue(config.hotkey_enabled)
            self.assertEqual(config.hotkey_stop_speaking, "alt+cmd+s")

    def test_disable_keeps_spec_but_flips_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            set_hotkey_config(config_path, enabled=True, stop_speaking="ctrl+alt+cmd+.")
            set_hotkey_config(config_path, enabled=False)
            config = load_config(config_path)
            self.assertFalse(config.hotkey_enabled)
            self.assertEqual(config.hotkey_stop_speaking, "ctrl+alt+cmd+.")

    def test_set_hotkey_config_rejects_invalid_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with self.assertRaises(ValueError):
                set_hotkey_config(config_path, stop_speaking="cmd+nope")

    def test_existing_config_gets_hotkey_section_appended(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("[user]\nlanguage = \"en\"\n", encoding="utf-8")
            write_default_config(config_path)
            text = config_path.read_text(encoding="utf-8")
            self.assertIn("[hotkey]", text)
            config = load_config(config_path)
            self.assertTrue(config.hotkey_enabled)
            self.assertEqual(config.hotkey_stop_speaking, "alt+cmd+s")


if __name__ == "__main__":
    unittest.main()
