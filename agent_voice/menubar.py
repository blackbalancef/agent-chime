from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import objc
    from AppKit import (
        NSAlert,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSButton,
        NSFont,
        NSImageLeft,
        NSImage,
        NSMenu,
        NSMenuItem,
        NSSlider,
        NSStatusBar,
        NSTextField,
        NSVariableStatusItemLength,
        NSView,
    )
    from Foundation import NSObject, NSTimer
    from PyObjCTools import AppHelper
except Exception as exc:  # pragma: no cover - platform dependent
    objc = None
    NSAlert = None
    NSApplication = None
    NSApplicationActivationPolicyAccessory = None
    NSButton = None
    NSFont = None
    NSImageLeft = None
    NSImage = None
    NSMenu = None
    NSMenuItem = None
    NSSlider = None
    NSStatusBar = None
    NSTextField = None
    NSVariableStatusItemLength = None
    NSView = None
    NSObject = object
    NSTimer = None
    AppHelper = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def _python_method(func):
    if objc is None:
        return func
    return objc.python_method(func)

from .config import (
    SUMMARY_MODEL_CHOICES,
    TTS_MODEL_CHOICES,
    VOICE_CHOICES,
    language_display_name,
    load_config,
    set_config_language,
    set_events_config,
    set_hotkey_config,
    set_summary_config,
    set_voice_config,
)
from .installer.claude_code import (
    install_claude_code_personal,
    remove_claude_code_personal,
)
from .installer.codex import install_codex_personal, remove_codex_personal
from .installer.pi import install_pi_personal, remove_pi_personal
from .secrets import (
    get_openai_secret_status,
    set_openai_keychain_secret,
    validate_openai_tts_key,
)
from .delivery import DeliveryRouter, test_message
from .doctor import inspect_agent_wiring
from .hotkey import (
    HOTKEY_PRESETS,
    GlobalHotkey,
    carbon_available,
    format_hotkey_display,
    parse_hotkey,
)
from .runtime import (
    clear_voice_mute,
    clear_voice_pid,
    is_pid_running,
    read_voice_activity_started_at,
    read_voice_pid,
    set_voice_mute,
    stop_speaking,
    voice_mute_status,
)
from .service import daemon_status, service_paths, start_daemon, stop_daemon
from .usage import (
    DashboardData,
    UsageStats,
    format_duration,
    format_usd,
    read_dashboard,
    read_last_voice_channel,
    sparkline,
)


AGENT_LABELS = {"claude-code": "Claude", "codex": "Codex", "pi": "Pi", "other": "Other"}
CHANNEL_LABELS = {"openai_tts": "OpenAI", "macos_say": "say"}

# Voice engine (backend) choices surfaced as a submenu, paired with a display
# label and the default voice to fall back to when switching into them.
VOICE_BACKEND_LABELS = {"macos_say": "macOS say", "openai_tts": "OpenAI TTS"}
VOICE_BACKEND_DEFAULT_VOICE = {"openai_tts": "marin", "macos_say": "Alex"}

# Lifecycle event toggles, paired with the config attribute exposed on
# AgentVoiceConfig so the checkmark reflects live state.
EVENT_TOGGLES: tuple[tuple[str, str, str], ...] = (
    ("task_finished", "notify_task_finished", "Task finished"),
    ("permission_needed", "notify_permission_needed", "Permission needed"),
    ("input_needed", "notify_input_needed", "Input needed"),
    ("task_failed", "notify_task_failed", "Task failed"),
    ("subagent_finished", "notify_subagent_finished", "Subagent finished"),
)

# Integrations surfaced in the submenu, paired with their installer/remover so a
# single handler can add or remove any agent by name.
INTEGRATIONS: tuple[tuple[str, str], ...] = (
    ("claude-code", "Claude Code"),
    ("codex", "Codex"),
    ("pi", "pi"),
)
# Registry keyed by agent → the module-level installer/remover *names*. They are
# resolved via ``globals()`` at call time (rather than captured here) so tests can
# patch ``agent_voice.menubar.install_*`` and have the handler honor the patch.
INTEGRATION_INSTALLERS = {
    "claude-code": "install_claude_code_personal",
    "codex": "install_codex_personal",
    "pi": "install_pi_personal",
}
INTEGRATION_REMOVERS = {
    "claude-code": "remove_claude_code_personal",
    "codex": "remove_codex_personal",
    "pi": "remove_pi_personal",
}


MENU_BAR_ICON_SIZE = 22.0
MENU_BAR_ICON_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "menubar"
ACTIVITY_FRAME_INTERVAL_SECONDS = 0.2
ACTIVITY_ICON_STATES = ("speaking-1", "speaking-2", "speaking-3", "speaking-2")
VOICE_SPEED_MIN = 0.25
VOICE_SPEED_MAX = 4.0
VOICE_SPEED_STEP = 0.05
VOICE_SPEED_SLIDER_WIDTH = 320.0
VOICE_SPEED_SLIDER_HEIGHT = 88.0
# One shared horizontal inset so the label, slider, and preset buttons line up.
VOICE_SPEED_CONTENT_INSET = 20.0
VOICE_SPEED_BUTTON_GAP = 8.0
VOICE_SPEED_PRESETS = (1.0, 1.5, 2.0)
# Slider value is carried on preset buttons via setTag_ (int), scaled by this factor.
VOICE_SPEED_TAG_SCALE = 100
# Raw NSEventType values; constant across AppKit and available without Cocoa.
LEFT_MOUSE_DOWN_EVENT_TYPE = 1
LEFT_MOUSE_DRAGGED_EVENT_TYPE = 6
NS_ALERT_FIRST_BUTTON_RETURN = 1000


def menu_voice_speed_value(speed: float) -> float:
    clamped = min(max(float(speed), VOICE_SPEED_MIN), VOICE_SPEED_MAX)
    steps = round((clamped - VOICE_SPEED_MIN) / VOICE_SPEED_STEP)
    return round(VOICE_SPEED_MIN + steps * VOICE_SPEED_STEP, 2)


def format_voice_speed(speed: float) -> str:
    return f"{menu_voice_speed_value(speed):.2f}x"


def voice_speed_label(speed: float) -> str:
    return f"Speed: {format_voice_speed(speed)}"


def format_speed_preset(speed: float) -> str:
    text = f"{float(speed):g}"
    return f"{text}×"


def speed_to_tag(speed: float) -> int:
    return round(float(speed) * VOICE_SPEED_TAG_SCALE)


def tag_to_speed(tag: int) -> float:
    return tag / VOICE_SPEED_TAG_SCALE


def is_slider_commit_event_type(event_type: int | None) -> bool:
    """Whether a slider action should persist the value (vs. only update the label).

    Continuous sliders fire on every drag tick; we persist and restart the daemon
    only when the interaction settles — on mouse-up or keyboard adjustment — never
    mid-drag.
    """
    if event_type is None:
        return True
    return event_type not in (LEFT_MOUSE_DOWN_EVENT_TYPE, LEFT_MOUSE_DRAGGED_EVENT_TYPE)


def format_countdown(seconds: int) -> str:
    seconds = max(0, seconds)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    if hour:
        return f"{hour}:{minute:02d}:{second:02d}"
    return f"{minute}:{second:02d}"


def mute_countdown(muted_until: int | None, *, now: float | None = None) -> str | None:
    if muted_until is None:
        return None
    current_time = time.time() if now is None else now
    remaining_seconds = int(max(0, muted_until - current_time))
    return format_countdown(remaining_seconds)


class AgentVoiceMenuBar(NSObject):
    config_path: str | None
    status_item: object
    status_images: dict[bool, object]
    activity_images: dict[bool, list[object]]
    timer: object
    animation_timer: object
    animation_frame_index: int
    active_voice_pid: int | None
    voice_activity_active: bool

    @_python_method
    def setup(self, config_path: str | None) -> None:
        self.config_path = config_path
        self.animation_frame_index = 0
        self.active_voice_pid = None
        self.voice_activity_active = False
        self.menu_open = False
        self.speed_label = None
        self.speed_slider = None
        self.last_shown_speed = None
        self.test_playing = False
        # Global stop-speaking hotkey (Carbon). Synced from config on each refresh.
        self.hotkey = None
        self.hotkey_spec = None
        self.hotkey_enabled_state = None
        self.hotkey_display = ""
        self.status_images = {
            False: self._make_status_image(muted=False),
            True: self._make_status_image(muted=True),
        }
        self.activity_images = {
            False: self._make_activity_images(muted=False),
            True: self._make_activity_images(muted=True),
        }
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            5.0,
            self,
            "refresh:",
            None,
            True,
        )
        self.animation_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            ACTIVITY_FRAME_INTERVAL_SECONDS,
            self,
            "animate:",
            None,
            True,
        )
        self.refresh()

    def refresh_(self, sender) -> None:
        self.refresh()

    def animate_(self, sender) -> None:
        self.animate()

    @_python_method
    def refresh(self) -> None:
        config = load_config(self.config_path)
        self._sync_hotkey(config)
        mute_status = voice_mute_status(config)
        voice_pid, voice_active = self._voice_activity(config)
        self._update_status_button(
            muted=mute_status.muted,
            mute_remaining=mute_countdown(mute_status.muted_until),
            voice_pid=voice_pid,
            voice_active=voice_active,
        )
        # Never swap the menu while the user has it open — doing so freezes the
        # popover and resets controls (e.g. the speed slider) mid-interaction.
        if not self.menu_open:
            self.status_item.setMenu_(
                self._build_menu(config, voice_pid=voice_pid, voice_active=voice_active)
            )
        self.active_voice_pid = voice_pid
        self.voice_activity_active = voice_active

    @_python_method
    def animate(self) -> None:
        config = load_config(self.config_path)
        mute_status = voice_mute_status(config)
        voice_pid, voice_active = self._voice_activity(config)
        if not voice_active:
            self.animation_frame_index = 0
        self._update_status_button(
            muted=mute_status.muted,
            mute_remaining=mute_countdown(mute_status.muted_until),
            voice_pid=voice_pid,
            voice_active=voice_active,
        )
        if voice_active:
            self.animation_frame_index = (self.animation_frame_index + 1) % len(ACTIVITY_ICON_STATES)
        if (
            not self.menu_open
            and (voice_pid != self.active_voice_pid or voice_active != self.voice_activity_active)
        ):
            self.status_item.setMenu_(
                self._build_menu(config, voice_pid=voice_pid, voice_active=voice_active)
            )
            self.active_voice_pid = voice_pid
            self.voice_activity_active = voice_active

    @_python_method
    def _update_status_button(
        self,
        *,
        muted: bool,
        mute_remaining: str | None,
        voice_pid: int | None,
        voice_active: bool,
    ) -> None:
        button = self.status_item.button()
        if button is None:
            return

        button.setTitle_(mute_remaining or "")
        if voice_active:
            frames = self.activity_images[muted]
            image = frames[self.animation_frame_index % len(frames)]
            tooltip = (
                f"Voiccce: speaking ({voice_pid})"
                if voice_pid
                else "Voiccce: preparing voice"
            )
        else:
            image = self.status_images[muted]
            tooltip = f"Voiccce: muted for {mute_remaining}" if mute_remaining else "Voiccce"
        button.setImage_(image)
        button.setImagePosition_(NSImageLeft)
        button.setToolTip_(tooltip)

    @_python_method
    def _voice_activity(self, config) -> tuple[int | None, bool]:
        voice_pid = self._active_voice_pid(config)
        return voice_pid, bool(voice_pid or read_voice_activity_started_at(config))

    @_python_method
    def _active_voice_pid(self, config) -> int | None:
        voice_pid = read_voice_pid(config)
        if voice_pid is None:
            return None
        if is_pid_running(voice_pid):
            return voice_pid
        clear_voice_pid(config, voice_pid)
        return None

    @_python_method
    def _make_status_image(self, *, muted: bool, activity_phase: int | None = None) -> object:
        if muted:
            return self._make_icon_asset("muted")
        if activity_phase is None:
            return self._make_icon_asset("listening")
        state = ACTIVITY_ICON_STATES[activity_phase % len(ACTIVITY_ICON_STATES)]
        return self._make_icon_asset(state)

    @_python_method
    def _make_activity_images(self, *, muted: bool) -> list[object]:
        return [
            self._make_status_image(muted=muted, activity_phase=phase)
            for phase in range(len(ACTIVITY_ICON_STATES))
        ]

    @_python_method
    def _make_icon_asset(self, state: str) -> object:
        path = MENU_BAR_ICON_ASSET_DIR / f"bat-{state}.svg"
        image = NSImage.alloc().initWithContentsOfFile_(str(path))
        if image is None:
            raise RuntimeError(f"Could not load menu bar icon asset: {path}")
        image.setSize_((MENU_BAR_ICON_SIZE, MENU_BAR_ICON_SIZE))
        image.setTemplate_(True)
        return image

    @_python_method
    def _build_menu(
        self,
        config,
        *,
        voice_pid: int | None = None,
        voice_active: bool | None = None,
    ) -> object:
        menu = NSMenu.alloc().init()
        menu.setDelegate_(self)
        self.speed_label = None
        self.speed_slider = None
        daemon_pid, daemon_running = daemon_status(config)
        mute_status = voice_mute_status(config)
        if voice_pid is None or voice_active is None:
            voice_pid, voice_active = self._voice_activity(config)

        daemon_label = "Daemon: running" + (f" ({daemon_pid})" if daemon_pid else "") if daemon_running else "Daemon: stopped"
        menu.addItem_(self._item(daemon_label, enabled=False))

        if mute_status.muted and mute_status.muted_until:
            until = datetime.fromtimestamp(mute_status.muted_until).strftime("%H:%M:%S")
            remaining = mute_countdown(mute_status.muted_until)
            voice_label = f"Voice: muted {remaining} left (until {until})"
        else:
            voice_label = "Voice: on"
        if voice_pid:
            voice_label += f" | speaking pid {voice_pid}"
        elif voice_active:
            voice_label += " | preparing audio"
        menu.addItem_(self._item(voice_label, enabled=False))
        self._add_voice_engine_items(menu, config)
        menu.addItem_(NSMenuItem.separatorItem())

        self._add_announce_events_submenu(menu, config)
        self._add_integrations_submenu(menu, config)
        menu.addItem_(NSMenuItem.separatorItem())

        self._add_dashboard_items(menu, config)
        menu.addItem_(NSMenuItem.separatorItem())

        stop_title = "Stop Speaking"
        if self.hotkey is not None and self.hotkey_display:
            stop_title = f"Stop Speaking  ({self.hotkey_display})"
        menu.addItem_(self._item(stop_title, "stopSpeaking:"))
        menu.addItem_(self._hotkey_submenu_item(config))
        menu.addItem_(self._item("Mute 10 min", "muteTenMinutes:"))
        menu.addItem_(self._item("Mute 1 hour", "muteOneHour:"))
        menu.addItem_(self._item("Unmute", "unmute:"))
        menu.addItem_(NSMenuItem.separatorItem())

        menu.addItem_(self._item("Start Daemon", "startDaemon:", enabled=not daemon_running))
        menu.addItem_(self._item("Stop Daemon", "stopDaemon:", enabled=daemon_running))
        menu.addItem_(NSMenuItem.separatorItem())

        menu.addItem_(self._item("Open Config", "openConfig:"))
        menu.addItem_(self._item("Open Daemon Log", "openDaemonLog:"))
        menu.addItem_(NSMenuItem.separatorItem())

        menu.addItem_(self._item("Quit Menu Bar", "quit:"))
        return menu

    @_python_method
    def _add_voice_engine_items(self, menu: object, config) -> None:
        last_channel = self._read_last_voice_channel(config)
        if last_channel == "openai_tts":
            last_label = "Last spoken: OpenAI TTS"
        elif last_channel == "macos_say":
            if config.voice_backend == "openai_tts":
                last_label = "Last spoken: macOS say ⚠️ (OpenAI fell back)"
            else:
                last_label = "Last spoken: macOS say"
        else:
            last_label = "Last spoken: —"
        menu.addItem_(self._item(last_label, enabled=False))

        self._add_voice_backend_submenu(menu, config)
        # Surface the key-update affordance prominently when an OpenAI delivery
        # quietly fell back to macOS say — that almost always means a bad/missing
        # key, and the fix is to re-enter it.
        if last_channel == "macos_say" and config.voice_backend == "openai_tts":
            menu.addItem_(self._item("Update OpenAI API key…", "updateOpenAIKey:"))

        self._add_picker_items(menu, config)

    @_python_method
    def _add_voice_backend_submenu(self, menu: object, config) -> None:
        current = config.voice_backend
        label = VOICE_BACKEND_LABELS.get(current, current)
        parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Voice engine: {label}", None, ""
        )
        submenu = NSMenu.alloc().init()
        for backend, backend_label in VOICE_BACKEND_LABELS.items():
            child = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                backend_label, "selectVoiceBackend:", ""
            )
            child.setTarget_(self)
            child.setRepresentedObject_(backend)
            child.setState_(1 if backend == current else 0)
            submenu.addItem_(child)
        # Always offer the key-update item inside the engine submenu so it can be
        # refreshed even when delivery has not fallen back yet.
        submenu.addItem_(NSMenuItem.separatorItem())
        submenu.addItem_(self._item("Update OpenAI API key…", "updateOpenAIKey:"))
        parent.setSubmenu_(submenu)
        menu.addItem_(parent)

    @_python_method
    def _add_picker_items(self, menu: object, config) -> None:
        menu.addItem_(
            self._item(
                f"Notification language: {language_display_name(config.language)}",
                "changeLanguage:",
            )
        )

        voice_choices = self._choices_with_current(
            VOICE_CHOICES.get(config.voice_backend, ()), config.voice_name
        )
        menu.addItem_(
            self._submenu_item(
                f"Voice: {config.voice_name or '—'}",
                voice_choices,
                config.voice_name,
                "selectVoice:",
            )
        )

        if config.voice_backend == "openai_tts":
            tts_choices = self._choices_with_current(TTS_MODEL_CHOICES, config.voice_model)
            menu.addItem_(
                self._submenu_item(
                    f"TTS Model: {config.voice_model}",
                    tts_choices,
                    config.voice_model,
                    "selectTtsModel:",
                )
            )
            menu.addItem_(self._voice_speed_slider_item(config))

        menu.addItem_(self._item("▶ Play test audio", "playTestAudio:"))

        summary_choices = self._choices_with_current(SUMMARY_MODEL_CHOICES, config.summary_model)
        menu.addItem_(
            self._submenu_item(
                f"Summary Model: {config.summary_model}",
                summary_choices,
                config.summary_model,
                "selectSummaryModel:",
                enabled=config.summary_enabled,
            )
        )

        reminders_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Announce idle reminders", "toggleIdleReminders:", ""
        )
        reminders_item.setTarget_(self)
        reminders_item.setState_(1 if config.notify_input_needed else 0)
        menu.addItem_(reminders_item)

        interrupt_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Stop audio when I reply", "toggleInterruptOnReply:", ""
        )
        interrupt_item.setTarget_(self)
        interrupt_item.setState_(1 if config.voice_interrupt_on_user_input else 0)
        menu.addItem_(interrupt_item)

    @_python_method
    def _add_announce_events_submenu(self, menu: object, config) -> None:
        parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Announce events", None, ""
        )
        submenu = NSMenu.alloc().init()
        for event_name, attr, label in EVENT_TOGGLES:
            child = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "toggleAnnounceEvent:", ""
            )
            child.setTarget_(self)
            child.setRepresentedObject_(event_name)
            child.setState_(1 if getattr(config, attr) else 0)
            submenu.addItem_(child)
        parent.setSubmenu_(submenu)
        menu.addItem_(parent)

    @_python_method
    def _add_integrations_submenu(self, menu: object, config) -> None:
        parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Integrations", None, ""
        )
        submenu = NSMenu.alloc().init()
        wired = {row.agent: row.wired for row in self._inspect_wiring(config)}
        for agent, label in INTEGRATIONS:
            is_wired = bool(wired.get(agent))
            title = f"{label}: {'wired' if is_wired else 'not wired'}"
            child = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, "toggleIntegration:", ""
            )
            child.setTarget_(self)
            child.setRepresentedObject_(agent)
            child.setState_(1 if is_wired else 0)
            submenu.addItem_(child)
        parent.setSubmenu_(submenu)
        menu.addItem_(parent)

    @_python_method
    def _inspect_wiring(self, config) -> list:
        try:
            return inspect_agent_wiring(config)
        except Exception as exc:
            self._log(f"Integration wiring read failed: {exc}")
            return []

    @_python_method
    def _hotkey_submenu_item(self, config) -> object:
        """A submenu to pick (or disable) the global stop-speaking hotkey."""
        enabled = config.hotkey_enabled
        current = config.hotkey_stop_speaking
        # Parse once: a hand-edited config can carry an invalid spec, which we want
        # to surface clearly rather than show a checkmark-less ghost entry.
        parsed_current = None
        if enabled and current:
            try:
                parsed_current = parse_hotkey(current)
            except ValueError:
                parsed_current = None

        if not enabled:
            title = "Stop hotkey: off"
        elif parsed_current is not None:
            title = f"Stop hotkey: {parsed_current.display}"
        else:
            title = f"Stop hotkey: {current} (invalid — pick one)"

        if not carbon_available():
            return self._item(f"{title} — unavailable on this Mac", enabled=False)

        parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        submenu = NSMenu.alloc().init()

        current_canonical = parsed_current.canonical if parsed_current is not None else None
        specs = list(HOTKEY_PRESETS)
        if current_canonical and current_canonical not in specs:
            specs.insert(0, current_canonical)
        for spec in specs:
            child = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                format_hotkey_display(spec), "selectStopHotkey:", ""
            )
            child.setTarget_(self)
            child.setRepresentedObject_(spec)
            child.setState_(1 if spec == current_canonical else 0)
            submenu.addItem_(child)

        submenu.addItem_(NSMenuItem.separatorItem())
        off = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Off", "selectStopHotkey:", "")
        off.setTarget_(self)
        off.setRepresentedObject_("off")
        off.setState_(0 if enabled else 1)
        submenu.addItem_(off)

        parent.setSubmenu_(submenu)
        return parent

    @_python_method
    def _choices_with_current(self, choices: tuple[str, ...], current: str | None) -> list[str]:
        result = list(choices)
        if current and current not in result:
            result.insert(0, current)
        return result

    @_python_method
    def _submenu_item(
        self,
        title: str,
        choices: list[str],
        current: str | None,
        action: str,
        *,
        enabled: bool = True,
    ) -> object:
        parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        parent.setEnabled_(enabled)
        submenu = NSMenu.alloc().init()
        for choice in choices:
            child = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(choice, action, "")
            child.setTarget_(self)
            child.setRepresentedObject_(choice)
            child.setState_(1 if choice == current else 0)
            child.setEnabled_(enabled)
            submenu.addItem_(child)
        parent.setSubmenu_(submenu)
        return parent

    @_python_method
    def _voice_speed_slider_item(self, config) -> object:
        title = voice_speed_label(config.voice_speed)
        if NSView is None or NSTextField is None or NSSlider is None:
            return self._item(title, enabled=False)

        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        view = NSView.alloc().initWithFrame_(
            ((0.0, 0.0), (VOICE_SPEED_SLIDER_WIDTH, VOICE_SPEED_SLIDER_HEIGHT))
        )

        inset = VOICE_SPEED_CONTENT_INSET
        content_width = VOICE_SPEED_SLIDER_WIDTH - inset * 2

        label = NSTextField.labelWithString_(title)
        label.setFrame_(((inset, 66.0), (content_width, 16.0)))
        if NSFont is not None:
            label.setFont_(NSFont.menuFontOfSize_(0.0))
        view.addSubview_(label)
        # Held so the slider action can update the number live during a drag.
        self.speed_label = label
        self.last_shown_speed = menu_voice_speed_value(config.voice_speed)

        slider = NSSlider.alloc().initWithFrame_(((inset, 42.0), (content_width, 20.0)))
        slider.setMinValue_(VOICE_SPEED_MIN)
        slider.setMaxValue_(VOICE_SPEED_MAX)
        slider.setDoubleValue_(menu_voice_speed_value(config.voice_speed))
        slider.setTarget_(self)
        slider.setAction_("voiceSpeedChanged:")
        slider.setContinuous_(True)
        slider.setToolTip_("OpenAI TTS speed")
        view.addSubview_(slider)
        self.speed_slider = slider

        self._add_speed_preset_buttons(view)

        item.setView_(view)
        return item

    @_python_method
    def _add_speed_preset_buttons(self, view: object) -> None:
        if NSButton is None:
            return
        inset = VOICE_SPEED_CONTENT_INSET
        gap = VOICE_SPEED_BUTTON_GAP
        content_width = VOICE_SPEED_SLIDER_WIDTH - inset * 2
        count = len(VOICE_SPEED_PRESETS)
        button_width = (content_width - gap * (count - 1)) / count
        for index, preset in enumerate(VOICE_SPEED_PRESETS):
            x = inset + index * (button_width + gap)
            button = NSButton.alloc().initWithFrame_(((x, 10.0), (button_width, 22.0)))
            button.setTitle_(format_speed_preset(preset))
            button.setBezelStyle_(1)  # NSBezelStyleRounded
            button.setTarget_(self)
            button.setAction_("voiceSpeedPreset:")
            button.setTag_(speed_to_tag(preset))
            button.setToolTip_(f"Set speed to {format_voice_speed(preset)}")
            if NSFont is not None:
                button.setFont_(NSFont.systemFontOfSize_(11.0))
            cell = button.cell()
            if cell is not None:
                cell.setControlSize_(1)  # NSControlSizeSmall
            view.addSubview_(button)

    @_python_method
    def _read_last_voice_channel(self, config) -> str | None:
        try:
            return read_last_voice_channel(config.database_path)
        except Exception as exc:
            self._log(f"Last channel read failed: {exc}")
            return None

    @_python_method
    def _add_dashboard_items(self, menu: object, config) -> None:
        menu.addItem_(self._item("Dashboard (spend)", enabled=False))
        data = self._read_dashboard(config)
        if data is None:
            menu.addItem_(self._item("Stats unavailable", enabled=False))
            return

        menu.addItem_(self._item(self._spend_line("Today", data.today), enabled=False))
        menu.addItem_(self._item(self._spend_line("7 days", data.last_7d), enabled=False))
        menu.addItem_(self._item(self._spend_line("30 days", data.last_30d), enabled=False))
        menu.addItem_(self._item(self._spend_line("All-time", data.all_time), enabled=False))

        spark = sparkline(data.spark_7d)
        if spark:
            menu.addItem_(self._item(f"7-day trend  {spark}", enabled=False))

        agent_line = self._breakdown_line(data.by_agent, AGENT_LABELS)
        if agent_line:
            menu.addItem_(self._item(f"By agent (today): {agent_line}", enabled=False))
        channel_line = self._breakdown_line(data.by_channel, CHANNEL_LABELS)
        if channel_line:
            menu.addItem_(self._item(f"By channel (today): {channel_line}", enabled=False))

    @_python_method
    def _spend_line(self, label: str, stats: UsageStats) -> str:
        total = stats.audio_cost_usd + stats.summary_cost_usd
        return (
            f"{label}: {format_usd(total)} · "
            f"{format_duration(stats.audio_duration_seconds)} · "
            f"{stats.reports_listened_count} spoken"
        )

    @_python_method
    def _breakdown_line(self, rows: list[tuple[str, float, int]], labels: dict[str, str]) -> str:
        parts = [
            f"{labels.get(key, key)} {format_usd(spend)} ({spoken})"
            for key, spend, spoken in rows
            if spoken > 0 or spend > 0
        ]
        return " · ".join(parts)

    @_python_method
    def _read_dashboard(self, config) -> DashboardData | None:
        try:
            return read_dashboard(config.database_path, config.timezone)
        except Exception as exc:
            self._log(f"Dashboard read failed: {exc}")
            return None

    @_python_method
    def _item(self, title: str, action: str | None = None, *, enabled: bool = True) -> object:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        item.setEnabled_(enabled)
        if action:
            item.setTarget_(self)
        return item

    @_python_method
    def _config(self):
        return load_config(self.config_path)

    @_python_method
    def _sync_hotkey(self, config) -> None:
        """Register/replace/remove the global hotkey to match the current config."""
        enabled = config.hotkey_enabled
        spec = config.hotkey_stop_speaking
        if enabled == self.hotkey_enabled_state and spec == self.hotkey_spec:
            return
        self._teardown_hotkey()
        if not enabled:
            self.hotkey_enabled_state = enabled
            self.hotkey_spec = spec
            return
        if not carbon_available():
            self.hotkey_enabled_state = enabled
            self.hotkey_spec = spec
            self._log("Global hotkey unavailable (Carbon framework not found)")
            return
        try:
            parsed = parse_hotkey(spec)
        except ValueError as exc:
            self.hotkey_enabled_state = enabled
            self.hotkey_spec = spec
            self._log(f"Invalid stop-speaking hotkey '{spec}': {exc}")
            return
        try:
            hotkey = GlobalHotkey()
            hotkey.register(parsed, self._on_stop_hotkey)
        except Exception as exc:
            # A registration failure can be transient (for example, another app
            # temporarily owns the combo), so do not mark this config as synced.
            self._log(f"Could not register stop-speaking hotkey {parsed.display}: {exc}")
            return
        self.hotkey = hotkey
        self.hotkey_enabled_state = enabled
        self.hotkey_spec = spec
        self.hotkey_display = parsed.display
        self._log(f"Stop-speaking hotkey registered: {parsed.display} ({parsed.canonical})")

    @_python_method
    def _teardown_hotkey(self) -> None:
        if self.hotkey is not None:
            try:
                self.hotkey.unregister()
            except Exception as exc:
                self._log(f"Hotkey unregister failed: {exc}")
        self.hotkey = None
        self.hotkey_display = ""

    @_python_method
    def _on_stop_hotkey(self) -> None:
        # Fired by Carbon on the main run loop. Stopping playback can briefly block
        # (SIGTERM then SIGKILL), so hand it to a worker thread to keep the UI snappy.
        self._log(f"Stop-speaking hotkey pressed ({self.hotkey_display})")
        threading.Thread(target=self._run_stop_speaking, daemon=True).start()

    @_python_method
    def _run_stop_speaking(self) -> None:
        try:
            pid = stop_speaking(self._config())
            self._log(f"Hotkey stop-speaking; pid={pid or '-'}")
        except Exception as exc:
            self._log(f"Hotkey stop-speaking failed: {exc}")

    def selectStopHotkey_(self, sender) -> None:
        spec = str(sender.representedObject())
        config = self._config()
        if spec == "off":
            set_hotkey_config(config.config_path, enabled=False)
            self._log("Stop-speaking hotkey disabled")
        else:
            try:
                set_hotkey_config(config.config_path, enabled=True, stop_speaking=spec)
            except ValueError as exc:
                self._log(f"Invalid hotkey '{spec}': {exc}")
                return
            self._log(f"Stop-speaking hotkey set to {format_hotkey_display(spec)}")
        # Apply immediately (no daemon restart — the hotkey lives in the menu bar).
        self._sync_hotkey(self._config())
        self.refresh()

    def stopSpeaking_(self, sender) -> None:
        pid = stop_speaking(self._config())
        self._log(f"Stop Speaking clicked; pid={pid or '-'}")
        self.refresh()

    def muteTenMinutes_(self, sender) -> None:
        muted_until = set_voice_mute(self._config(), 10 * 60)
        self._log(f"Mute 10 min clicked; muted_until={muted_until}")
        self.refresh()

    def muteOneHour_(self, sender) -> None:
        muted_until = set_voice_mute(self._config(), 60 * 60)
        self._log(f"Mute 1 hour clicked; muted_until={muted_until}")
        self.refresh()

    def unmute_(self, sender) -> None:
        clear_voice_mute(self._config())
        self._log("Unmute clicked")
        self.refresh()

    def startDaemon_(self, sender) -> None:
        pid = start_daemon(self._config())
        self._log(f"Start Daemon clicked; pid={pid}")
        self.refresh()

    def stopDaemon_(self, sender) -> None:
        pid = stop_daemon(self._config())
        self._log(f"Stop Daemon clicked; pid={pid or '-'}")
        self.refresh()

    def selectVoice_(self, sender) -> None:
        voice = str(sender.representedObject())
        config = self._config()
        set_voice_config(config.config_path, voice=voice)
        self._restart_daemon_if_running()
        self._log(f"Voice set to {voice}")
        self.refresh()

    def selectTtsModel_(self, sender) -> None:
        model = str(sender.representedObject())
        config = self._config()
        set_voice_config(config.config_path, model=model)
        self._restart_daemon_if_running()
        self._log(f"TTS model set to {model}")
        self.refresh()

    def selectVoiceBackend_(self, sender) -> None:
        backend = str(sender.representedObject())
        config = self._config()
        if backend == config.voice_backend:
            return
        if backend == "openai_tts" and not get_openai_secret_status(config).available:
            # Switching to OpenAI without a usable key would silently fall back to
            # say. Prompt for one first and only commit the backend if it sticks.
            if not self._prompt_and_store_openai_key():
                self._log("Voice engine unchanged (no OpenAI key provided)")
                self.refresh()
                return
            config = self._config()
        voice = config.voice_name
        if not voice or voice in VOICE_BACKEND_DEFAULT_VOICE.values():
            # Carry over a sensible default voice when crossing backends, since
            # macOS and OpenAI voice names are disjoint.
            voice = VOICE_BACKEND_DEFAULT_VOICE.get(backend, voice)
        set_voice_config(config.config_path, backend=backend, voice=voice)
        self._restart_daemon_if_running()
        self._log(f"Voice engine set to {VOICE_BACKEND_LABELS.get(backend, backend)}")
        self.refresh()

    def updateOpenAIKey_(self, sender) -> None:
        if self._prompt_and_store_openai_key():
            self._restart_daemon_if_running()
        self.refresh()

    @_python_method
    def _prompt_and_store_openai_key(self) -> bool:
        """Prompt for an OpenAI key, validate it, and store it in the Keychain.

        Returns True when a key was validated and saved. Returns False on cancel,
        empty input, validation failure, or when the alert UI is unavailable —
        the caller decides whether to commit a dependent change (e.g. the backend
        switch). Restarting the daemon is left to the caller.
        """
        key = self._prompt_openai_key()
        if not key:
            return False
        config = self._config()
        validation = validate_openai_tts_key(config, key)
        if not validation.ok:
            self._log(f"OpenAI key validation failed: {validation.error}")
            self._alert_message(
                "OpenAI key rejected",
                validation.error or "The key could not generate test audio.",
            )
            return False
        try:
            set_openai_keychain_secret(config, key)
        except RuntimeError as exc:
            self._log(f"Could not save OpenAI key: {exc}")
            self._alert_message(
                "Could not save OpenAI key",
                f"{exc}\n\nPut it in ~/.voiccce/.env as OPENAI_API_KEY=... instead.",
            )
            return False
        self._log("OpenAI key saved to macOS Keychain")
        return True

    @_python_method
    def _prompt_openai_key(self) -> str | None:
        if NSAlert is None or NSTextField is None:
            return None
        alert = NSAlert.alloc().init()
        alert.setMessageText_("OpenAI API key")
        alert.setInformativeText_(
            "Paste an OpenAI API key. It is validated with a short TTS generation "
            "and stored in your macOS Keychain."
        )
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        field = NSTextField.alloc().initWithFrame_(((0.0, 0.0), (280.0, 24.0)))
        field.setStringValue_("")
        alert.setAccessoryView_(field)
        if alert.runModal() != NS_ALERT_FIRST_BUTTON_RETURN:
            return None
        return str(field.stringValue()).strip() or None

    @_python_method
    def _alert_message(self, title: str, detail: str) -> None:
        if NSAlert is None:
            return
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(detail)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    def toggleAnnounceEvent_(self, sender) -> None:
        event_name = str(sender.representedObject())
        attr = next((a for name, a, _ in EVENT_TOGGLES if name == event_name), None)
        if attr is None:
            self._log(f"Unknown announce-event toggle '{event_name}'")
            return
        config = self._config()
        new_value = not getattr(config, attr)
        set_events_config(config.config_path, **{event_name: new_value})
        self._restart_daemon_if_running()
        self._log(f"Announce {event_name} {'enabled' if new_value else 'disabled'}")
        self.refresh()

    def toggleIntegration_(self, sender) -> None:
        agent = str(sender.representedObject())
        config = self._config()
        wired = {row.agent: row.wired for row in self._inspect_wiring(config)}
        if wired.get(agent):
            self._remove_integration(agent)
        else:
            self._add_integration(agent)
        self.refresh()

    @_python_method
    def _add_integration(self, agent: str) -> None:
        installer_name = INTEGRATION_INSTALLERS.get(agent)
        if installer_name is None:
            self._log(f"Unknown integration '{agent}'")
            return
        installer = globals()[installer_name]
        try:
            installer(config_path=self._config().config_path)
        except Exception as exc:
            self._log(f"Could not wire {agent}: {exc}")
            self._alert_message(f"Could not wire {agent}", str(exc))
            return
        self._restart_daemon_if_running()
        self._log(f"Wired integration {agent}")

    @_python_method
    def _remove_integration(self, agent: str) -> None:
        remover_name = INTEGRATION_REMOVERS.get(agent)
        if remover_name is None:
            self._log(f"Unknown integration '{agent}'")
            return
        remover = globals()[remover_name]
        try:
            remover()
        except Exception as exc:
            self._log(f"Could not remove {agent}: {exc}")
            self._alert_message(f"Could not remove {agent}", str(exc))
            return
        self._restart_daemon_if_running()
        self._log(f"Removed integration {agent}")

    def voiceSpeedChanged_(self, sender) -> None:
        speed = menu_voice_speed_value(float(sender.doubleValue()))
        # A continuous slider fires on every pixel of travel; only redraw the label
        # when the quantized value actually changes, otherwise dragging spams the
        # main thread with redundant relayouts and stutters.
        if speed != self.last_shown_speed:
            self.last_shown_speed = speed
            if self.speed_label is not None:
                self.speed_label.setStringValue_(voice_speed_label(speed))
        # Live drag ticks only redraw the label; persist once the drag settles so we
        # restart the daemon a single time instead of on every intermediate value.
        if not is_slider_commit_event_type(self._current_event_type()):
            return
        sender.setDoubleValue_(speed)
        self._persist_voice_speed(speed)

    def voiceSpeedPreset_(self, sender) -> None:
        speed = menu_voice_speed_value(tag_to_speed(sender.tag()))
        self.last_shown_speed = speed
        if self.speed_label is not None:
            self.speed_label.setStringValue_(voice_speed_label(speed))
        if self.speed_slider is not None:
            self.speed_slider.setDoubleValue_(speed)
        self._persist_voice_speed(speed)

    @_python_method
    def _persist_voice_speed(self, speed: float) -> None:
        config = self._config()
        if speed == config.voice_speed:
            return
        set_voice_config(config.config_path, speed=speed)
        # No daemon restart: it hot-reloads config each poll cycle, so the new speed
        # applies within ~0.5s without freezing the menu on every slider release.
        self._log(f"Voice speed set to {format_voice_speed(speed)}")

    @_python_method
    def _current_event_type(self) -> int | None:
        if NSApplication is None:
            return None
        event = NSApplication.sharedApplication().currentEvent()
        return None if event is None else int(event.type())

    def menuWillOpen_(self, menu) -> None:
        self.menu_open = True

    def menuDidClose_(self, menu) -> None:
        self.menu_open = False

    def playTestAudio_(self, sender) -> None:
        if self.test_playing:
            self._log("Test audio already playing")
            return
        self.test_playing = True
        # TTS synthesis + playback take seconds — run off the main thread so the menu
        # never freezes while testing the current voice/speed.
        thread = threading.Thread(target=self._run_test_audio, daemon=True)
        thread.start()

    @_python_method
    def _run_test_audio(self) -> None:
        try:
            config = self._config()
            self._log(
                f"Playing test audio (voice {config.voice_name or '—'}, "
                f"{format_voice_speed(config.voice_speed)})"
            )
            result = DeliveryRouter(config).deliver_configured_voice(test_message(config))
            if result.spoken:
                self._log(f"Test audio played via {result.channel}")
            else:
                detail = f": {result.error}" if result.error else ""
                self._log(f"Test audio not spoken (channel {result.channel}{detail})")
        except Exception as exc:
            self._log(f"Test audio failed: {exc}")
        finally:
            self.test_playing = False

    def selectSummaryModel_(self, sender) -> None:
        model = str(sender.representedObject())
        config = self._config()
        set_summary_config(config.config_path, model=model)
        self._restart_daemon_if_running()
        self._log(f"Summary model set to {model}")
        self.refresh()

    def changeLanguage_(self, sender) -> None:
        config = self._config()
        language = self._prompt_language(config.language)
        if language is None:
            return
        try:
            set_config_language(config.config_path, language)
        except ValueError as exc:
            self._log(f"Notification language not changed: {exc}")
            return
        self._restart_daemon_if_running()
        self._log(f"Notification language set to {language_display_name(self._config().language)}")
        self.refresh()

    @_python_method
    def _prompt_language(self, current: str) -> str | None:
        if NSAlert is None or NSTextField is None:
            return None

        alert = NSAlert.alloc().init()
        alert.setMessageText_("Notification language")
        alert.setInformativeText_("Type any language name, e.g. English, Russian, Spanish, or Japanese.")
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")

        field = NSTextField.alloc().initWithFrame_(((0.0, 0.0), (280.0, 24.0)))
        field.setStringValue_(language_display_name(current))
        alert.setAccessoryView_(field)

        if alert.runModal() != NS_ALERT_FIRST_BUTTON_RETURN:
            return None
        value = str(field.stringValue()).strip()
        return value or None

    def toggleIdleReminders_(self, sender) -> None:
        config = self._config()
        new_value = not config.notify_input_needed
        set_events_config(config.config_path, input_needed=new_value)
        self._restart_daemon_if_running()
        self._log(f"Idle reminders {'enabled' if new_value else 'disabled'}")
        self.refresh()

    def toggleInterruptOnReply_(self, sender) -> None:
        config = self._config()
        new_value = not config.voice_interrupt_on_user_input
        set_voice_config(config.config_path, interrupt_on_user_input=new_value)
        # Read fresh by the hook on each invocation — no daemon restart needed.
        self._log(f"Stop-audio-on-reply {'enabled' if new_value else 'disabled'}")
        self.refresh()

    @_python_method
    def _restart_daemon_if_running(self) -> None:
        config = self._config()
        _, running = daemon_status(config)
        if not running:
            return
        stop_daemon(config)
        start_daemon(config)
        self._log("Daemon restarted to apply config change")

    def openConfig_(self, sender) -> None:
        config = self._config()
        subprocess.run(["open", str(config.config_path)], check=False)

    def openDaemonLog_(self, sender) -> None:
        paths = service_paths(self._config())
        paths.log_path.parent.mkdir(parents=True, exist_ok=True)
        paths.log_path.touch(exist_ok=True)
        subprocess.run(["open", str(paths.log_path)], check=False)

    def quit_(self, sender) -> None:
        self._log("Quit Menu Bar clicked")
        self._teardown_hotkey()
        NSApplication.sharedApplication().terminate_(self)

    @_python_method
    def _log(self, message: str) -> None:
        print(f"[voiccce menubar] {datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


def run_menubar(config_path: str | Path | None = None) -> None:
    if _IMPORT_ERROR is not None:
        raise RuntimeError(
            "Menu bar requires pyobjc-framework-Cocoa. Install with "
            "`pip install pyobjc-framework-Cocoa` or `pipx inject voiccce pyobjc-framework-Cocoa`."
        ) from _IMPORT_ERROR

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    controller = AgentVoiceMenuBar.alloc().init()
    controller.setup(str(config_path) if config_path else None)
    app.setDelegate_(controller)
    AppHelper.runEventLoop()
