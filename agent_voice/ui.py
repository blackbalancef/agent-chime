"""Dependency-free interactive terminal UI for Voiccce.

Arrow-key navigation with checkbox (multi-select) and radio (single-select)
prompts, styled with ANSI. Uses only the standard library (``termios``/``tty``
for raw input, ANSI escapes for in-place rendering) so the installer stays
zero-dependency. When stdin or stdout is not a TTY, prompts return defaults.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
import unicodedata
from dataclasses import dataclass, field

__all__ = ["Choice", "MultiSelect", "checkbox_select", "select_one", "confirm", "style"]


# ─── color styling ───────────────────────────────────────────────────────────

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

_FG_CYAN = "\033[96m"
_FG_GREEN = "\033[92m"
_FG_GRAY = "\033[90m"
_FG_YELLOW = "\033[93m"
_FG_RED = "\033[91m"
_FG_MAGENTA = "\033[95m"

# Glyphs whose rendered width terminals disagree on with unicodedata.
_WIDE_GLYPHS = frozenset("🔔◉◯●○✓✗→↔↕❯❮◆◇■□▲▼✦✧⚠")


def color_supported(stream: object = sys.stdout) -> bool:
    """Best-effort detection of ANSI color support."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("CLICOLOR_FORCE") == "1":
        return True
    if os.environ.get("CLICOLOR") == "0":
        return False
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty and isatty())
    except Exception:
        return False


class style:
    """Style helpers that no-op when color is disabled."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap(_BOLD, text)

    def dim(self, text: str) -> str:
        return self._wrap(_DIM, text)

    def cyan(self, text: str) -> str:
        return self._wrap(_FG_CYAN, text)

    def green(self, text: str) -> str:
        return self._wrap(_FG_GREEN, text)

    def gray(self, text: str) -> str:
        return self._wrap(_FG_GRAY, text)

    def yellow(self, text: str) -> str:
        return self._wrap(_FG_YELLOW, text)

    def red(self, text: str) -> str:
        return self._wrap(_FG_RED, text)

    def magenta(self, text: str) -> str:
        return self._wrap(_FG_MAGENTA, text)

    def kbd(self, text: str) -> str:
        """Style a keycap, e.g. ``↑`` or ``space``."""
        return self._wrap(_BOLD, text)


# ─── string widths ───────────────────────────────────────────────────────────

def display_width(text: str) -> int:
    """Rendered width of ``text`` ignoring ANSI escape codes."""
    width = 0
    in_escape = False
    for ch in text:
        if ch == "\033":
            in_escape = True
            continue
        if in_escape:
            if ch == "m":
                in_escape = False
            continue
        if ch in _WIDE_GLYPHS:
            width += 2
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def pad_to(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


# ─── data model ──────────────────────────────────────────────────────────────

@dataclass
class Choice:
    """One row in a prompt: a value, a label, and an optional description."""

    value: str
    label: str
    description: str = ""
    hint: str = ""


@dataclass
class _State:
    choices: list[Choice]
    cursor: int = 0
    selected: set[str] = field(default_factory=set)


# ─── selector widget ─────────────────────────────────────────────────────────

class MultiSelect:
    """Arrow-key selector supporting both checkbox (multi) and radio (single) modes.

    Keys: ``↑``/``↓`` (or ``k``/``j``) move, ``space`` toggles/selects,
    ``a`` toggles all (multi only), ``enter`` confirms, ``esc``/``Ctrl-C`` cancels.
    """

    def __init__(
        self,
        choices: list[Choice],
        *,
        mode: str = "multi",
        title: str = "",
        subtitle: str = "",
        default: list[str] | None = None,
        min_selected: int = 0,
        confirm_label: str = "confirm",
        stream: object | None = None,
        get_key: "object | None" = None,
        interactive: bool | None = None,
    ) -> None:
        if not choices:
            raise ValueError("MultiSelect needs at least one choice")
        if mode not in ("multi", "single"):
            raise ValueError(f"unknown mode {mode!r}")
        self.choices = choices
        self.mode = mode
        self.title = title
        self.subtitle = subtitle
        self.confirm_label = confirm_label
        self.stream = stream if stream is not None else sys.stdout
        self._get_key = get_key
        self.colors = style(color_supported(self.stream))

        if mode == "single":
            cursor = 0
            if default:
                for i, ch in enumerate(choices):
                    if ch.value == default[0]:
                        cursor = i
                        break
            self._state = _State(choices=choices, cursor=cursor, selected={choices[cursor].value})
            self.min_selected = 1
        else:
            self._state = _State(choices=choices, selected=set(default or ()))
            self.min_selected = max(0, min(min_selected, len(choices)))

        if interactive is None:
            interactive = self._detect_interactive()
        self.interactive = bool(interactive)
        self._drawn = 0

    def _detect_interactive(self) -> bool:
        try:
            stdin_tty = bool(sys.stdin.isatty())
        except Exception:
            stdin_tty = False
        try:
            return stdin_tty and bool(self.stream.isatty())
        except Exception:
            return False

    # ── rendering ────────────────────────────────────────────────────────────

    def _terminal_width(self) -> int:
        try:
            import shutil

            return shutil.get_terminal_size((80, 24)).columns
        except Exception:
            return 80

    def _card_width(self) -> int:
        base = display_width(self.title or "")
        if self.subtitle:
            base += display_width(self.subtitle) + 4
        return max(base + 10, 44)

    def _hint_line(self) -> str:
        c = self.colors
        if self.mode == "single":
            pairs = [("↑↓", "navigate"), ("↵", "select"), ("esc", "cancel")]
        else:
            pairs = [
                ("↑↓", "navigate"),
                ("space", "toggle"),
                ("a", "all"),
                ("↵", self.confirm_label),
                ("esc", "cancel"),
            ]
        sep = c.gray("  ·  ")
        parts = [f"{c.kbd(k)} {c.dim(lbl)}" for k, lbl in pairs]
        return "  " + sep.join(parts)

    def _selection_summary(self) -> str:
        c = self.colors
        if self.mode == "single":
            cur = self.choices[self._state.cursor]
            return f"{c.dim('selected:')} {c.green(c.bold(cur.label))}"
        n = len(self._state.selected)
        if n == 0:
            return c.dim("nothing selected")
        names = sorted(ch.label for ch in self.choices if ch.value in self._state.selected)
        return f"{c.bold(str(n))} {c.dim('selected:')} {c.green(', '.join(names))}"

    def _title_card(self) -> str:
        c = self.colors
        width = self._card_width()
        inner = width - 2
        label = f"🔔  {c.bold(self.title)}"
        if self.subtitle:
            label += f"  {c.gray('·')}  {c.dim(self.subtitle)}"
        top = c.gray("╭" + "─" * inner + "╮")
        mid = c.gray("│") + pad_to(label, inner) + c.gray("│")
        bot = c.gray("╰" + "─" * inner + "╯")
        return f"{top}\n{mid}\n{bot}"

    def _divider(self) -> str:
        c = self.colors
        width = max(24, min(self._terminal_width() - 2, 58))
        return "  " + c.gray("─" * width)

    def _build_frame(self) -> list[str]:
        c = self.colors
        lines: list[str] = [""]
        if self.title:
            lines.append(self._title_card())
        lines.append("")

        st = self._state
        for i, choice in enumerate(st.choices):
            active = i == st.cursor
            checked = choice.value in st.selected
            marker = c.cyan(c.bold("❯")) if active else " "
            if self.mode == "single":
                box = c.green("●") if active else c.gray("○")
                name = c.bold(choice.label) if active else choice.label
            elif checked:
                box = c.green("◉")
                name = c.bold(choice.label)
            else:
                box = c.gray("◯")
                name = choice.label
            line = f"  {marker} {box}  {name}"
            if choice.hint:
                line += "  " + c.dim(choice.hint)
            lines.append(line)
            if choice.description:
                lines.append(f"      {c.dim(choice.description)}")
            lines.append("")

        lines.append(self._divider())
        lines.append("  " + self._selection_summary())
        lines.append("  " + self._hint_line())
        return lines

    def _render(self) -> None:
        out = self.stream
        # Flatten multi-line entries (e.g. the title card) into physical lines.
        # In raw mode ``tty.setraw`` clears ONLCR, so a bare "\n" is a line feed
        # with no carriage return: a multi-line string would staircase to the
        # right, and counting it as one line would desync the cursor-up redraw.
        # Splitting here keeps every emitted line column-0 anchored and makes
        # ``_drawn`` the true physical-line count.
        lines: list[str] = []
        for entry in self._build_frame():
            lines.extend(entry.split("\n"))
        if self._drawn > 0:
            out.write(f"\033[{self._drawn}A")
        for line in lines:
            out.write("\r")
            out.write("\033[K")
            out.write(line)
            out.write("\n")
        out.write("\r")
        out.write("\033[J")
        out.flush()
        self._drawn = len(lines)

    # ── input ────────────────────────────────────────────────────────────────

    def _next_key(self) -> str:
        if self._get_key is not None:
            key = self._get_key()
            return key if isinstance(key, str) else str(key)
        return self._read_key_raw()

    def _read_key_raw(self) -> str:
        fd = sys.stdin.fileno()
        first = os.read(fd, 1)
        if not first:
            return "escape"  # EOF
        if first == b"\x1b":
            seq = self._read_escape_tail(fd)
            mapping = {
                b"[A": "up",
                b"[B": "down",
                b"[C": "right",
                b"[D": "left",
            }
            if seq in mapping:
                return mapping[seq]
            return "escape"
        if first in (b"\r", b"\n"):
            return "enter"
        if first == b" ":
            return "space"
        if first == b"\x03":
            raise KeyboardInterrupt
        return first.decode("latin1")

    @staticmethod
    def _read_escape_tail(fd: int, timeout: float = 0.03) -> bytes:
        seq = b""
        while len(seq) < 4:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                break
            chunk = os.read(fd, 1)
            if not chunk:
                break
            seq += chunk
        return seq

    # ── loop ─────────────────────────────────────────────────────────────────

    def run(self) -> list[str] | None:
        """Run the prompt; returns chosen values or ``None`` if cancelled."""
        if not self.interactive:
            return sorted(self._state.selected) or None
        real_terminal = self._get_key is None
        if real_terminal:
            self._hide_cursor()
        try:
            self._enter_raw_mode()
            return self._loop()
        finally:
            if real_terminal:
                self._restore_terminal()
                self._show_cursor()

    _termios_fd: int | None = None
    _termios_old: object | None = None

    def _enter_raw_mode(self) -> None:
        if self._get_key is not None:
            return  # tests supply their own key source
        try:
            fd = sys.stdin.fileno()
            self._termios_fd = fd
            self._termios_old = termios.tcgetattr(fd)
            tty.setraw(fd)
        except Exception:
            self._termios_fd = None
            self._termios_old = None

    def _restore_terminal(self) -> None:
        if self._termios_old is not None and self._termios_fd is not None:
            try:
                termios.tcsetattr(self._termios_fd, termios.TCSADRAIN, self._termios_old)
            except Exception:
                pass

    def _hide_cursor(self) -> None:
        try:
            self.stream.write("\033[?25l")
            self.stream.flush()
        except Exception:
            pass

    def _show_cursor(self) -> None:
        try:
            self.stream.write("\033[?25h")
            self.stream.flush()
        except Exception:
            pass

    def _loop(self) -> list[str] | None:
        n = len(self.choices)
        while True:
            self._state.cursor = max(0, min(self._state.cursor, n - 1))
            if self.mode == "single":
                self._state.selected = {self.choices[self._state.cursor].value}
            self._render()
            try:
                key = self._next_key()
            except KeyboardInterrupt:
                self._cancel_cleanup()
                return None

            if key in ("up", "k"):
                self._state.cursor = (self._state.cursor - 1) % n
            elif key in ("down", "j"):
                self._state.cursor = (self._state.cursor + 1) % n
            elif key in ("space", "right"):
                if self.mode == "single":
                    if len(self._state.selected) >= self.min_selected:
                        self._confirm_cleanup()
                        return sorted(self._state.selected)
                else:
                    self._toggle(self._state.cursor)
            elif key == "a" and self.mode == "multi":
                self._toggle_all()
            elif key == "enter":
                if len(self._state.selected) >= self.min_selected:
                    self._confirm_cleanup()
                    return sorted(self._state.selected)
            elif key in ("escape", "q"):
                self._cancel_cleanup()
                return None

    def _toggle(self, index: int) -> None:
        value = self.choices[index].value
        if value in self._state.selected:
            self._state.selected.discard(value)
        else:
            self._state.selected.add(value)

    def _toggle_all(self) -> None:
        all_values = {ch.value for ch in self.choices}
        if self._state.selected >= all_values:
            self._state.selected = set()
        else:
            self._state.selected = set(all_values)

    def _confirm_cleanup(self) -> None:
        self.stream.write("\n")
        self.stream.flush()

    def _cancel_cleanup(self) -> None:
        out = self.stream
        if self._drawn > 0:
            out.write(f"\033[{self._drawn}A")
        out.write("\r")
        out.write("\033[J")
        out.write(f"  {self.colors.dim('Cancelled.')}\n")
        out.flush()
        self._drawn = 0


# ─── convenience helpers ─────────────────────────────────────────────────────

def checkbox_select(
    choices: list[Choice],
    *,
    title: str = "",
    subtitle: str = "",
    default: list[str] | None = None,
    min_selected: int = 0,
    confirm_label: str = "confirm",
    stream: object | None = None,
    get_key: "object | None" = None,
    interactive: bool | None = None,
) -> list[str] | None:
    """Run a checkbox multi-select; returns chosen values or ``None``."""
    return MultiSelect(
        choices,
        mode="multi",
        title=title,
        subtitle=subtitle,
        default=default,
        min_selected=min_selected,
        confirm_label=confirm_label,
        stream=stream,
        get_key=get_key,
        interactive=interactive,
    ).run()


def select_one(
    choices: list[Choice],
    *,
    title: str = "",
    subtitle: str = "",
    default: str | None = None,
    stream: object | None = None,
    get_key: "object | None" = None,
    interactive: bool | None = None,
) -> str | None:
    """Run a radio single-select; returns the chosen value or ``None``."""
    default_list = [default] if default else None
    result = MultiSelect(
        choices,
        mode="single",
        title=title,
        subtitle=subtitle,
        default=default_list,
        stream=stream,
        get_key=get_key,
        interactive=interactive,
    ).run()
    return result[0] if result else None


def confirm(
    question: str,
    *,
    default: bool = True,
    stream: object | None = None,
    get_key: "object | None" = None,
    interactive: bool | None = None,
) -> bool:
    """Arrow-key yes/no prompt. Returns ``default`` on non-TTY or cancel."""
    result = select_one(
        [Choice(value="yes", label="Yes"), Choice(value="no", label="No")],
        title=question,
        default="yes" if default else "no",
        stream=stream,
        get_key=get_key,
        interactive=interactive,
    )
    if result is None:
        return default
    return result == "yes"
