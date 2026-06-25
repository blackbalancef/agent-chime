import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_voice import menubar as menubar_module
from agent_voice.config import load_config
from agent_voice.menubar import (
    ACTIVITY_FRAME_INTERVAL_SECONDS,
    ACTIVITY_ICON_STATES,
    LEFT_MOUSE_DOWN_EVENT_TYPE,
    LEFT_MOUSE_DRAGGED_EVENT_TYPE,
    VOICE_SPEED_PRESETS,
    AgentVoiceMenuBar,
    format_countdown,
    format_speed_preset,
    format_voice_speed,
    is_slider_commit_event_type,
    menu_voice_speed_value,
    mute_countdown,
    speed_to_tag,
    tag_to_speed,
    voice_speed_label,
)


class MenuBarTests(unittest.TestCase):
    def test_format_countdown(self) -> None:
        self.assertEqual(format_countdown(0), "0:00")
        self.assertEqual(format_countdown(9), "0:09")
        self.assertEqual(format_countdown(65), "1:05")
        self.assertEqual(format_countdown(3605), "1:00:05")

    def test_mute_countdown_uses_remaining_seconds(self) -> None:
        self.assertEqual(mute_countdown(700, now=100), "10:00")
        self.assertEqual(mute_countdown(700, now=640), "1:00")
        self.assertEqual(mute_countdown(700, now=701), "0:00")
        self.assertIsNone(mute_countdown(None, now=100))

    def test_menu_voice_speed_value_clamps_and_steps(self) -> None:
        self.assertEqual(menu_voice_speed_value(0.1), 0.25)
        self.assertEqual(menu_voice_speed_value(4.5), 4.0)
        self.assertEqual(menu_voice_speed_value(1.234), 1.25)
        self.assertEqual(menu_voice_speed_value(1.224), 1.2)

    def test_format_voice_speed(self) -> None:
        self.assertEqual(format_voice_speed(1), "1.00x")
        self.assertEqual(format_voice_speed(1.2), "1.20x")

    def test_voice_speed_label(self) -> None:
        self.assertEqual(voice_speed_label(1), "Speed: 1.00x")
        self.assertEqual(voice_speed_label(1.234), "Speed: 1.25x")

    def test_slider_commits_on_mouse_up_and_keyboard_not_mid_drag(self) -> None:
        # Mid-drag ticks (mouse-down/dragged) must not persist the value.
        self.assertFalse(is_slider_commit_event_type(LEFT_MOUSE_DOWN_EVENT_TYPE))
        self.assertFalse(is_slider_commit_event_type(LEFT_MOUSE_DRAGGED_EVENT_TYPE))
        # Mouse-up (2), keyboard (10), and "no event" settle the value.
        self.assertTrue(is_slider_commit_event_type(2))
        self.assertTrue(is_slider_commit_event_type(10))
        self.assertTrue(is_slider_commit_event_type(None))

    def test_format_speed_preset_strips_trailing_zeros(self) -> None:
        self.assertEqual(format_speed_preset(1.0), "1×")
        self.assertEqual(format_speed_preset(1.5), "1.5×")
        self.assertEqual(format_speed_preset(2.0), "2×")

    def test_preset_tag_round_trip(self) -> None:
        for preset in VOICE_SPEED_PRESETS:
            self.assertEqual(tag_to_speed(speed_to_tag(preset)), preset)

    def test_speaking_animation_uses_nested_frame_sequence(self) -> None:
        self.assertGreaterEqual(ACTIVITY_FRAME_INTERVAL_SECONDS, 0.18)
        self.assertLessEqual(ACTIVITY_FRAME_INTERVAL_SECONDS, 0.25)
        self.assertEqual(
            ACTIVITY_ICON_STATES,
            ("speaking-1", "speaking-2", "speaking-3", "speaking-2"),
        )
        self.assertEqual(
            tuple(ACTIVITY_ICON_STATES[index % len(ACTIVITY_ICON_STATES)] for index in range(7)),
            (
                "speaking-1",
                "speaking-2",
                "speaking-3",
                "speaking-2",
                "speaking-1",
                "speaking-2",
                "speaking-3",
            ),
        )

    def test_speaking_wave_paths_are_nested_rotated_wifi_arcs(self) -> None:
        asset_dir = Path(__file__).resolve().parents[1] / "agent_voice" / "assets" / "menubar"
        expected_wave_counts = {
            "bat-speaking-1.svg": 1,
            "bat-speaking-2.svg": 2,
            "bat-speaking-3.svg": 3,
        }
        full_frame_paths = None
        previous_midpoint = None
        previous_radius = None
        origin = (14.55, 9.45)
        normalized_reference = None
        for filename, expected_count in expected_wave_counts.items():
            text = (asset_dir / filename).read_text(encoding="utf-8")
            wave_paths = [
                line.split('d="', 1)[1].split('"', 1)[0]
                for line in text.splitlines()
                if 'stroke-width="0.52"' in line
            ]
            self.assertEqual(len(wave_paths), expected_count)
            if full_frame_paths is None and filename == "bat-speaking-3.svg":
                full_frame_paths = wave_paths

        full_frame_text = (asset_dir / "bat-speaking-3.svg").read_text(encoding="utf-8")
        full_frame_paths = [
            line.split('d="', 1)[1].split('"', 1)[0]
            for line in full_frame_text.splitlines()
            if 'stroke-width="0.52"' in line
        ]
        for filename, expected_count in expected_wave_counts.items():
            text = (asset_dir / filename).read_text(encoding="utf-8")
            wave_paths = [
                line.split('d="', 1)[1].split('"', 1)[0]
                for line in text.splitlines()
                if 'stroke-width="0.52"' in line
            ]
            self.assertEqual(wave_paths, full_frame_paths[:expected_count])

        for path in full_frame_paths:
            start_text, rest = path.removeprefix("M").split(" C", 1)
            values = [float(value) for value in f"{start_text} {rest}".split()]
            points = tuple(zip(values[0::2], values[1::2], strict=True))
            radius = ((points[0][0] - origin[0]) ** 2 + (points[0][1] - origin[1]) ** 2) ** 0.5
            normalized = tuple(
                (
                    round((point[0] - origin[0]) / radius, 2),
                    round((point[1] - origin[1]) / radius, 2),
                )
                for point in points
            )
            if normalized_reference is None:
                normalized_reference = normalized
            else:
                for point, reference_point in zip(normalized, normalized_reference, strict=True):
                    self.assertAlmostEqual(point[0], reference_point[0], delta=0.02)
                    self.assertAlmostEqual(point[1], reference_point[1], delta=0.02)
            midpoint = self._cubic_midpoint(points)
            if previous_midpoint is not None and previous_radius is not None:
                self.assertGreater(radius, previous_radius)
                self.assertGreater(midpoint[0], previous_midpoint[0])
                self.assertLess(midpoint[1], previous_midpoint[1])
            previous_radius = radius
            previous_midpoint = midpoint

    @staticmethod
    def _cubic_midpoint(points: tuple[tuple[float, float], ...]) -> tuple[float, float]:
        start, control_1, control_2, end = points
        return (
            0.125 * start[0] + 0.375 * control_1[0] + 0.375 * control_2[0] + 0.125 * end[0],
            0.125 * start[1] + 0.375 * control_1[1] + 0.375 * control_2[1] + 0.125 * end[1],
        )


class _RecordingHotkey:
    """Stand-in for GlobalHotkey that records register/unregister calls."""

    def __init__(self) -> None:
        self.registered_with = None
        self.callback = None
        self.unregister_calls = 0

    def register(self, parsed, callback) -> None:
        self.registered_with = parsed
        self.callback = callback

    def unregister(self) -> None:
        self.unregister_calls += 1


@unittest.skipUnless(
    menubar_module._IMPORT_ERROR is not None,
    "drives the controller as a plain object; only valid when PyObjC is absent",
)
class HotkeyLifecycleTests(unittest.TestCase):
    """Exercise the menu bar's hotkey registration logic without PyObjC.

    When PyObjC is unavailable the NSObject base collapses to ``object`` and the
    ``@_python_method`` decorator is the identity, so the controller is a plain
    Python object whose hotkey methods can be called directly.
    """

    def _controller(self, config_path=None) -> AgentVoiceMenuBar:
        controller = AgentVoiceMenuBar.__new__(AgentVoiceMenuBar)
        controller.config_path = str(config_path) if config_path else None
        controller.hotkey = None
        controller.hotkey_spec = None
        controller.hotkey_enabled_state = None
        controller.hotkey_display = ""
        return controller

    def _config(self, *, enabled=True, spec="alt+cmd+s", config_path=None):
        return SimpleNamespace(
            hotkey_enabled=enabled,
            hotkey_stop_speaking=spec,
            config_path=config_path,
        )

    def test_sync_registers_when_enabled(self) -> None:
        created: list[_RecordingHotkey] = []
        with (
            patch("agent_voice.menubar.carbon_available", return_value=True),
            patch("agent_voice.menubar.GlobalHotkey", side_effect=lambda: created.append(_RecordingHotkey()) or created[-1]),
        ):
            controller = self._controller()
            controller._sync_hotkey(self._config())
        self.assertEqual(len(created), 1)
        self.assertIs(controller.hotkey, created[0])
        self.assertEqual(created[0].registered_with.canonical, "alt+cmd+s")
        self.assertEqual(controller.hotkey_display, "⌥⌘S")

    def test_sync_is_noop_when_unchanged(self) -> None:
        created: list[_RecordingHotkey] = []
        with (
            patch("agent_voice.menubar.carbon_available", return_value=True),
            patch("agent_voice.menubar.GlobalHotkey", side_effect=lambda: created.append(_RecordingHotkey()) or created[-1]),
        ):
            controller = self._controller()
            controller._sync_hotkey(self._config())
            controller._sync_hotkey(self._config())  # identical → must not re-register
        self.assertEqual(len(created), 1)

    def test_sync_replaces_on_change(self) -> None:
        created: list[_RecordingHotkey] = []
        with (
            patch("agent_voice.menubar.carbon_available", return_value=True),
            patch("agent_voice.menubar.GlobalHotkey", side_effect=lambda: created.append(_RecordingHotkey()) or created[-1]),
        ):
            controller = self._controller()
            controller._sync_hotkey(self._config(spec="alt+cmd+s"))
            controller._sync_hotkey(self._config(spec="ctrl+alt+cmd+."))
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].unregister_calls, 1)  # old one torn down
        self.assertEqual(created[1].registered_with.canonical, "ctrl+alt+cmd+.")
        self.assertIs(controller.hotkey, created[1])

    def test_sync_tears_down_when_disabled(self) -> None:
        created: list[_RecordingHotkey] = []
        with (
            patch("agent_voice.menubar.carbon_available", return_value=True),
            patch("agent_voice.menubar.GlobalHotkey", side_effect=lambda: created.append(_RecordingHotkey()) or created[-1]),
        ):
            controller = self._controller()
            controller._sync_hotkey(self._config(enabled=True))
            controller._sync_hotkey(self._config(enabled=False))
        self.assertEqual(created[0].unregister_calls, 1)
        self.assertIsNone(controller.hotkey)
        self.assertEqual(controller.hotkey_display, "")

    def test_sync_skips_invalid_spec(self) -> None:
        with (
            patch("agent_voice.menubar.carbon_available", return_value=True),
            patch("agent_voice.menubar.GlobalHotkey") as factory,
        ):
            controller = self._controller()
            controller._sync_hotkey(self._config(spec="typo+cmd+s"))
        factory.assert_not_called()
        self.assertIsNone(controller.hotkey)

    def test_sync_skips_when_carbon_unavailable(self) -> None:
        with (
            patch("agent_voice.menubar.carbon_available", return_value=False),
            patch("agent_voice.menubar.GlobalHotkey") as factory,
        ):
            controller = self._controller()
            controller._sync_hotkey(self._config())
        factory.assert_not_called()
        self.assertIsNone(controller.hotkey)

    def test_sync_handles_registration_failure(self) -> None:
        failing = MagicMock()
        failing.register.side_effect = OSError("combo in use")
        with (
            patch("agent_voice.menubar.carbon_available", return_value=True),
            patch("agent_voice.menubar.GlobalHotkey", return_value=failing),
        ):
            controller = self._controller()
            controller._sync_hotkey(self._config())  # must not raise
        self.assertIsNone(controller.hotkey)
        self.assertEqual(controller.hotkey_display, "")

    def test_sync_retries_after_registration_failure(self) -> None:
        failing = MagicMock()
        failing.register.side_effect = OSError("combo in use")
        succeeding = _RecordingHotkey()
        with (
            patch("agent_voice.menubar.carbon_available", return_value=True),
            patch("agent_voice.menubar.GlobalHotkey", side_effect=[failing, succeeding]) as factory,
        ):
            controller = self._controller()
            controller._sync_hotkey(self._config())
            controller._sync_hotkey(self._config())
        self.assertEqual(factory.call_count, 2)
        self.assertIs(controller.hotkey, succeeding)
        self.assertEqual(succeeding.registered_with.canonical, "alt+cmd+s")

    def test_run_stop_speaking_invokes_runtime(self) -> None:
        controller = self._controller()
        controller._config = lambda: SimpleNamespace()  # bypass real config load
        with patch("agent_voice.menubar.stop_speaking", return_value=4321) as stop:
            controller._run_stop_speaking()
        stop.assert_called_once()

    def test_select_stop_hotkey_persists_and_resyncs(self) -> None:
        created: list[_RecordingHotkey] = []
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            controller = self._controller(config_path=config_path)
            controller.refresh = lambda: None  # avoid full menu rebuild
            sender = SimpleNamespace(representedObject=lambda: "ctrl+alt+cmd+.")
            with (
                patch("agent_voice.menubar.carbon_available", return_value=True),
                patch("agent_voice.menubar.GlobalHotkey", side_effect=lambda: created.append(_RecordingHotkey()) or created[-1]),
            ):
                controller.selectStopHotkey_(sender)
            config = load_config(config_path)
            self.assertTrue(config.hotkey_enabled)
            self.assertEqual(config.hotkey_stop_speaking, "ctrl+alt+cmd+.")
            self.assertEqual(created[-1].registered_with.canonical, "ctrl+alt+cmd+.")

    def test_select_stop_hotkey_off_disables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            controller = self._controller(config_path=config_path)
            controller.refresh = lambda: None
            sender = SimpleNamespace(representedObject=lambda: "off")
            with (
                patch("agent_voice.menubar.carbon_available", return_value=True),
                patch("agent_voice.menubar.GlobalHotkey"),
            ):
                controller.selectStopHotkey_(sender)
            self.assertFalse(load_config(config_path).hotkey_enabled)

    def test_change_language_persists_and_restarts_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            controller = self._controller(config_path=config_path)
            controller.refresh = lambda: None
            controller._prompt_language = lambda current: "Spanish"
            controller._restart_daemon_if_running = MagicMock()

            controller.changeLanguage_(None)

            self.assertEqual(load_config(config_path).language, "Spanish")
            controller._restart_daemon_if_running.assert_called_once()

    def test_prompt_language_returns_entered_value(self) -> None:
        class FakeTextField:
            def __init__(self) -> None:
                self.value = ""

            @classmethod
            def alloc(cls):
                return cls()

            def initWithFrame_(self, _frame):
                return self

            def setStringValue_(self, value) -> None:
                self.value = str(value)

            def stringValue(self):
                return "Spanish"

        class FakeAlert:
            @classmethod
            def alloc(cls):
                return cls()

            def init(self):
                return self

            def setMessageText_(self, _value) -> None:
                pass

            def setInformativeText_(self, _value) -> None:
                pass

            def addButtonWithTitle_(self, _value) -> None:
                pass

            def setAccessoryView_(self, view) -> None:
                self.view = view

            def runModal(self) -> int:
                return menubar_module.NS_ALERT_FIRST_BUTTON_RETURN

        controller = self._controller()
        with (
            patch("agent_voice.menubar.NSAlert", FakeAlert),
            patch("agent_voice.menubar.NSTextField", FakeTextField),
        ):
            self.assertEqual(controller._prompt_language("en"), "Spanish")


if __name__ == "__main__":
    unittest.main()
