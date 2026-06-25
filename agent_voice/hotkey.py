"""Global keyboard hotkey for instantly stopping voice playback.

The hotkey is registered with Carbon's ``RegisterEventHotKey`` via ``ctypes``.
That API gives a true system-wide shortcut that works in any focused app and
needs **no Accessibility/Input-Monitoring permission** — the OS only routes our
exact combination to us, it never sees other keystrokes. (Modern PyObjC dropped
its Carbon bindings, hence the small ctypes shim.)

This module has two halves:

* Pure parsing/formatting (``parse_hotkey``, ``format_hotkey_display``) that turn
  a spec string like ``"alt+cmd+s"`` into a Carbon keycode + modifier mask and a
  display label like ``⌥⌘S``. These are stdlib-only and fully unit-tested.
* ``GlobalHotkey``, a thin ctypes wrapper that registers the combo and fires a
  callback on the main run loop. macOS-only; degrades gracefully elsewhere.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from dataclasses import dataclass

__all__ = [
    "DEFAULT_STOP_SPEAKING_HOTKEY",
    "HOTKEY_PRESETS",
    "ParsedHotkey",
    "GlobalHotkey",
    "parse_hotkey",
    "format_hotkey_display",
    "carbon_available",
]


# The combo Voiccce ships with. Configurable via [hotkey].stop_speaking.
DEFAULT_STOP_SPEAKING_HOTKEY = "alt+cmd+s"

# Curated combos offered in the menu bar picker and the setup wizard. All use
# multiple modifiers so a global registration won't steal a key from focused
# apps. The current value is always offered too, even if it is not listed here.
HOTKEY_PRESETS: tuple[str, ...] = (
    "alt+cmd+s",
    "ctrl+alt+cmd+s",
    "ctrl+alt+cmd+.",
    "alt+cmd+.",
)


# ─── modifiers ─────────────────────────────────────────────────────────────────
# Carbon modifier bit masks (Carbon/Events.h).
_CONTROL = 0x1000
_OPTION = 0x0800
_SHIFT = 0x0200
_CMD = 0x0100

# Apple's canonical display order: Control, Option, Shift, Command.
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd")
_MODIFIER_FLAG = {"ctrl": _CONTROL, "alt": _OPTION, "shift": _SHIFT, "cmd": _CMD}
_MODIFIER_SYMBOL = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
_MODIFIER_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl", "⌃": "ctrl",
    "alt": "alt", "opt": "alt", "option": "alt", "⌥": "alt",
    "shift": "shift", "⇧": "shift",
    "cmd": "cmd", "command": "cmd", "meta": "cmd", "super": "cmd", "win": "cmd", "⌘": "cmd",
}


# ─── keys ───────────────────────────────────────────────────────────────────────
# canonical name -> virtual keycode (ANSI), display label, and reverse aliases.
_KEY_CODES: dict[str, int] = {}
_KEY_DISPLAY: dict[str, str] = {}
_KEY_ALIASES: dict[str, str] = {}


def _register_key(canonical: str, keycode: int, display: str, *aliases: str) -> None:
    _KEY_CODES[canonical] = keycode
    _KEY_DISPLAY[canonical] = display
    for name in (canonical, *aliases):
        _KEY_ALIASES[name] = canonical


_LETTER_CODES = {
    "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E, "f": 0x03, "g": 0x05,
    "h": 0x04, "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25, "m": 0x2E, "n": 0x2D,
    "o": 0x1F, "p": 0x23, "q": 0x0C, "r": 0x0F, "s": 0x01, "t": 0x11, "u": 0x20,
    "v": 0x09, "w": 0x0D, "x": 0x07, "y": 0x10, "z": 0x06,
}
for _letter, _code in _LETTER_CODES.items():
    _register_key(_letter, _code, _letter.upper())

_DIGIT_CODES = {
    "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15,
    "5": 0x17, "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
}
for _digit, _code in _DIGIT_CODES.items():
    _register_key(_digit, _code, _digit)

_FUNCTION_CODES = {
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61,
    "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    "f13": 0x69, "f14": 0x6B, "f15": 0x71, "f16": 0x6A, "f17": 0x40, "f18": 0x4F,
    "f19": 0x50, "f20": 0x5A,
}
for _fkey, _code in _FUNCTION_CODES.items():
    _register_key(_fkey, _code, _fkey.upper())

# (canonical, keycode, display, *aliases)
_NAMED_KEYS: tuple[tuple[str, int, str, tuple[str, ...]], ...] = (
    ("-", 0x1B, "-", ("minus",)),
    ("=", 0x18, "=", ("equal", "equals")),
    ("[", 0x21, "[", ("leftbracket",)),
    ("]", 0x1E, "]", ("rightbracket",)),
    ("\\", 0x2A, "\\", ("backslash",)),
    (";", 0x29, ";", ("semicolon",)),
    ("'", 0x27, "'", ("quote", "apostrophe")),
    (",", 0x2B, ",", ("comma",)),
    (".", 0x2F, ".", ("period", "dot")),
    ("/", 0x2C, "/", ("slash",)),
    ("`", 0x32, "`", ("grave", "backtick")),
    ("space", 0x31, "Space", ("spacebar", "spc")),
    ("return", 0x24, "Return", ("enter", "ret")),
    ("tab", 0x30, "Tab", ()),
    ("escape", 0x35, "Esc", ("esc",)),
    ("delete", 0x33, "Delete", ("backspace", "del")),
    ("forwarddelete", 0x75, "Fwd Del", ("forward_delete",)),
    ("home", 0x73, "Home", ()),
    ("end", 0x77, "End", ()),
    ("pageup", 0x74, "Page Up", ("pgup",)),
    ("pagedown", 0x79, "Page Down", ("pgdn",)),
    ("left", 0x7B, "←", ("leftarrow",)),
    ("right", 0x7C, "→", ("rightarrow",)),
    ("up", 0x7E, "↑", ("uparrow",)),
    ("down", 0x7D, "↓", ("downarrow",)),
)
for _canon, _code, _disp, _aliases in _NAMED_KEYS:
    _register_key(_canon, _code, _disp, *_aliases)


@dataclass(frozen=True, slots=True)
class ParsedHotkey:
    """A validated hotkey: keycode + Carbon modifier mask, plus display forms."""

    keycode: int
    carbon_modifiers: int
    canonical: str
    display: str
    modifiers: tuple[str, ...]
    key: str


def parse_hotkey(spec: str) -> ParsedHotkey:
    """Parse ``"alt+cmd+s"`` into a :class:`ParsedHotkey`.

    Tokens are split on ``+`` and matched case-insensitively in any order. At
    least one modifier and exactly one non-modifier key are required — a global
    hotkey without a modifier would steal a bare key from every app.

    Raises ``ValueError`` for empty, modifier-only, multi-key, or unknown specs.
    """
    if not isinstance(spec, str):
        raise ValueError("hotkey must be a string")
    tokens = [token.strip().lower() for token in spec.split("+")]
    tokens = [token for token in tokens if token]
    if not tokens:
        raise ValueError("hotkey is empty")

    modifiers: list[str] = []
    keys: list[str] = []
    for token in tokens:
        if token in _MODIFIER_ALIASES:
            canonical = _MODIFIER_ALIASES[token]
            if canonical not in modifiers:
                modifiers.append(canonical)
        else:
            keys.append(token)

    if not keys:
        raise ValueError(f"hotkey '{spec}' needs a key besides modifiers, e.g. 'alt+cmd+s'")
    if len(keys) > 1:
        raise ValueError(f"hotkey '{spec}' must have exactly one non-modifier key, got {keys}")
    if not modifiers:
        raise ValueError(f"hotkey '{spec}' needs at least one modifier (cmd, ctrl, alt, or shift)")

    canonical_key = _KEY_ALIASES.get(keys[0])
    if canonical_key is None:
        raise ValueError(f"unknown key '{keys[0]}' in hotkey '{spec}'")

    ordered = [name for name in _MODIFIER_ORDER if name in modifiers]
    carbon_modifiers = 0
    for name in ordered:
        carbon_modifiers |= _MODIFIER_FLAG[name]

    canonical = "+".join([*ordered, canonical_key])
    display = "".join(_MODIFIER_SYMBOL[name] for name in ordered) + _KEY_DISPLAY[canonical_key]
    return ParsedHotkey(
        keycode=_KEY_CODES[canonical_key],
        carbon_modifiers=carbon_modifiers,
        canonical=canonical,
        display=display,
        modifiers=tuple(ordered),
        key=canonical_key,
    )


def format_hotkey_display(spec: str) -> str:
    """Pretty symbol form (``⌥⌘S``) of a spec; returns it unchanged if invalid."""
    try:
        return parse_hotkey(spec).display
    except ValueError:
        return spec


# ─── Carbon binding ──────────────────────────────────────────────────────────────

_K_EVENT_CLASS_KEYBOARD = 0x6B657962  # 'keyb'
_K_EVENT_HOTKEY_PRESSED = 6
_HOTKEY_SIGNATURE = 0x564F4943  # 'VOIC'

_carbon: ctypes.CDLL | None = None


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


# OSStatus handler(nextHandler, eventRef, userData)
_HANDLER_PROTO = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


def _load_carbon() -> ctypes.CDLL:
    global _carbon
    if _carbon is not None:
        return _carbon
    if sys.platform != "darwin":
        raise OSError("global hotkeys require macOS")
    name = ctypes.util.find_library("Carbon")
    if not name:
        raise OSError("Carbon framework not found")
    lib = ctypes.CDLL(name)
    lib.GetApplicationEventTarget.restype = ctypes.c_void_p
    lib.RegisterEventHotKey.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.RegisterEventHotKey.restype = ctypes.c_int32
    lib.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
    lib.UnregisterEventHotKey.restype = ctypes.c_int32
    lib.InstallEventHandler.argtypes = [
        ctypes.c_void_p, _HANDLER_PROTO, ctypes.c_uint32,
        ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.InstallEventHandler.restype = ctypes.c_int32
    lib.RemoveEventHandler.argtypes = [ctypes.c_void_p]
    lib.RemoveEventHandler.restype = ctypes.c_int32
    _carbon = lib
    return lib


def carbon_available() -> bool:
    """Whether a system-wide Carbon hotkey can be registered on this machine."""
    try:
        _load_carbon()
    except OSError:
        return False
    return True


class GlobalHotkey:
    """One system-wide hotkey backed by Carbon. macOS-only; no permission needed.

    Carbon dispatches ``kEventHotKeyPressed`` to the application event target,
    which is serviced by the main CFRunLoop — the same loop the menu bar app
    runs — so ``callback`` fires on the main thread. Keep the instance alive for
    as long as the hotkey should work, then call :meth:`unregister`.
    """

    def __init__(self) -> None:
        self._carbon: ctypes.CDLL | None = None
        # The CFUNCTYPE wrapper must be kept referenced; if it is collected the
        # C side calls into freed memory and crashes the process.
        self._handler_upp: object | None = None
        self._handler_ref = ctypes.c_void_p()
        self._hotkey_ref = ctypes.c_void_p()
        self._installed = False
        self._registered = False
        self._callback = None

    def register(self, parsed: ParsedHotkey, callback) -> None:
        """Register ``parsed`` and fire ``callback`` (no args) on each press.

        Calling again on the same instance swaps to the new combo. Raises
        ``OSError`` if Carbon is unavailable or the combo cannot be registered
        (e.g. already owned by another app).
        """
        carbon = _load_carbon()
        self._carbon = carbon
        self._callback = callback

        target = carbon.GetApplicationEventTarget()
        if not target:
            raise OSError("no application event target (is an NSApplication running?)")

        if not self._installed:
            def _trampoline(_next_handler, _event_ref, _user_data):
                try:
                    handler = self._callback
                    if handler is not None:
                        handler()
                except Exception:  # never let a Python error unwind into C
                    pass
                return 0  # noErr — we handled the hotkey

            self._handler_upp = _HANDLER_PROTO(_trampoline)
            spec = _EventTypeSpec(_K_EVENT_CLASS_KEYBOARD, _K_EVENT_HOTKEY_PRESSED)
            status = carbon.InstallEventHandler(
                target, self._handler_upp, 1, ctypes.byref(spec), None, ctypes.byref(self._handler_ref)
            )
            if status != 0:
                self._handler_upp = None
                raise OSError(f"InstallEventHandler failed (status {status})")
            self._installed = True

        # Drop any previous registration before binding the new combo.
        if self._registered and self._hotkey_ref:
            carbon.UnregisterEventHotKey(self._hotkey_ref)
            self._hotkey_ref = ctypes.c_void_p()
            self._registered = False

        hotkey_id = _EventHotKeyID(_HOTKEY_SIGNATURE, 1)
        status = carbon.RegisterEventHotKey(
            parsed.keycode, parsed.carbon_modifiers, hotkey_id, target, 0, ctypes.byref(self._hotkey_ref)
        )
        if status != 0:
            # Roll back the handler we may have just installed so a failed combo
            # (e.g. one already owned by another app) never leaks a Carbon handler.
            self.unregister()
            raise OSError(f"RegisterEventHotKey failed (status {status}); the combo may already be in use")
        self._registered = True

    def unregister(self) -> None:
        """Tear down the hotkey and its event handler. Safe to call repeatedly."""
        if self._carbon is None:
            return
        if self._registered and self._hotkey_ref:
            self._carbon.UnregisterEventHotKey(self._hotkey_ref)
            self._hotkey_ref = ctypes.c_void_p()
            self._registered = False
        if self._installed and self._handler_ref:
            self._carbon.RemoveEventHandler(self._handler_ref)
            self._handler_ref = ctypes.c_void_p()
            self._installed = False
        self._handler_upp = None
        self._callback = None
