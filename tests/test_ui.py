import unittest
from io import StringIO

from agent_voice.ui import (
    Choice,
    MultiSelect,
    checkbox_select,
    color_supported,
    confirm,
    display_width,
    pad_to,
    select_one,
)


def keys(seq):
    it = iter(seq)

    def get_key():
        try:
            return next(it)
        except StopIteration:
            return "enter"

    return get_key


CHOICES = [
    Choice("a", "Alpha", "first option"),
    Choice("b", "Beta"),
    Choice("c", "Gamma", "third option"),
]


class WidthTests(unittest.TestCase):
    def test_plain_width(self):
        self.assertEqual(display_width("hello"), 5)

    def test_ignores_ansi(self):
        self.assertEqual(display_width("\033[1mhi\033[0m"), 2)

    def test_wide_emoji_counts_as_two(self):
        self.assertEqual(display_width("🔔x"), 3)

    def test_pad_to_fills_spaces(self):
        self.assertEqual(pad_to("ab", 5), "ab   ")


class ColorDetectionTests(unittest.TestCase):
    def test_stringio_disables_color(self):
        self.assertFalse(color_supported(StringIO()))

    def test_force_enables_color(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"CLICOLOR_FORCE": "1"}, clear=True):
            self.assertTrue(color_supported(StringIO()))

    def test_no_color_disables(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            self.assertFalse(color_supported(StringIO()))


class MultiSelectLogicTests(unittest.TestCase):
    def test_non_interactive_returns_defaults(self):
        result = checkbox_select(
            CHOICES, default=["a", "c"], stream=StringIO(), interactive=False
        )
        self.assertEqual(result, ["a", "c"])

    def test_non_interactive_empty_returns_none(self):
        self.assertIsNone(checkbox_select(CHOICES, stream=StringIO(), interactive=False))

    def test_toggle_and_confirm(self):
        result = checkbox_select(
            CHOICES,
            default=["a"],
            stream=StringIO(),
            get_key=keys(["down", "space", "enter"]),
            interactive=True,
        )
        self.assertEqual(result, ["a", "b"])

    def test_arrow_wrap_around(self):
        result = checkbox_select(
            CHOICES,
            default=[],
            stream=StringIO(),
            # up from index 0 wraps to last (c), toggle, confirm
            get_key=keys(["up", "space", "enter"]),
            interactive=True,
        )
        self.assertEqual(result, ["c"])

    def test_toggle_all_selects_then_deselects(self):
        result = checkbox_select(
            CHOICES,
            default=[],
            stream=StringIO(),
            get_key=keys(["a", "enter"]),
            interactive=True,
        )
        self.assertEqual(result, ["a", "b", "c"])

    def test_cancel_returns_none(self):
        out = StringIO()
        result = checkbox_select(
            CHOICES, default=["a"], stream=out, get_key=keys(["escape"]), interactive=True
        )
        self.assertIsNone(result)
        self.assertIn("Cancelled.", out.getvalue())

    def test_min_selected_blocks_confirm(self):
        # enter with 0 selected is a no-op; then toggle then confirm works.
        result = checkbox_select(
            CHOICES,
            default=[],
            min_selected=1,
            stream=StringIO(),
            get_key=keys(["enter", "space", "enter"]),
            interactive=True,
        )
        self.assertEqual(result, ["a"])

    def test_empty_choices_raises(self):
        with self.assertRaises(ValueError):
            MultiSelect([], stream=StringIO(), interactive=False)


class FrameRenderingTests(unittest.TestCase):
    def test_cursor_and_checked_glyphs_render(self):
        widget = MultiSelect(
            CHOICES, default=["b"], stream=StringIO(), interactive=False
        )
        widget._state.cursor = 1  # highlight Beta, which is also checked
        frame = widget._build_frame()
        joined = "\n".join(frame)
        self.assertIn("❯ ◉  Beta", joined)
        self.assertIn("◯  Alpha", joined)
        self.assertIn("◯  Gamma", joined)

    def test_hint_line_multi(self):
        widget = MultiSelect(CHOICES, stream=StringIO(), interactive=False)
        hint = widget._hint_line()
        self.assertIn("space", hint)
        self.assertIn("all", hint)

    def test_hint_line_single(self):
        widget = MultiSelect(CHOICES, mode="single", stream=StringIO(), interactive=False)
        hint = widget._hint_line()
        self.assertIn("select", hint)
        self.assertNotIn("toggle", hint)

    def test_title_card_borders_aligned(self):
        widget = MultiSelect(
            CHOICES, title="Voiccce setup", subtitle="pick", stream=StringIO(), interactive=False
        )
        card = widget._title_card().split("\n")
        widths = {display_width(line) for line in card}
        self.assertEqual(len(widths), 1)  # all three box lines same width

    def test_render_counts_physical_lines_including_title_card(self):
        # Regression: the title card is one "\n"-joined entry in the frame. In a
        # raw-mode terminal a bare "\n" is line-feed only, so the card's lines must
        # each be emitted on their own carriage-return-anchored line and counted
        # individually — otherwise the cursor-up redraw desyncs and frames stack
        # ("staircase" of repeated title cards).
        out = StringIO()
        widget = MultiSelect(
            CHOICES,
            title="Voiccce setup",
            subtitle="Choose what to wire hooks for",
            stream=out,
            get_key=keys([]),
            interactive=True,
        )
        widget._render()
        physical = sum(entry.count("\n") + 1 for entry in widget._build_frame())
        self.assertGreater(physical, len(widget._build_frame()))  # card spans >1 line
        self.assertEqual(widget._drawn, physical)
        self.assertEqual(out.getvalue().count("\r\033[K"), physical)


class SingleSelectTests(unittest.TestCase):
    def test_navigation_then_enter(self):
        result = select_one(
            CHOICES,
            default="a",
            stream=StringIO(),
            get_key=keys(["down", "down", "enter"]),
            interactive=True,
        )
        self.assertEqual(result, "c")

    def test_cancel_returns_none(self):
        result = select_one(
            CHOICES,
            default="a",
            stream=StringIO(),
            get_key=keys(["escape"]),
            interactive=True,
        )
        self.assertIsNone(result)


class ConfirmTests(unittest.TestCase):
    def test_default_yes_on_cancel(self):
        self.assertTrue(
            confirm("ok?", default=True, stream=StringIO(), get_key=keys(["escape"]), interactive=True)
        )

    def test_choose_no(self):
        self.assertFalse(
            confirm("ok?", default=True, stream=StringIO(), get_key=keys(["down", "enter"]), interactive=True)
        )

    def test_non_tty_returns_default(self):
        self.assertTrue(confirm("ok?", default=True, stream=StringIO(), interactive=False))
        self.assertFalse(confirm("ok?", default=False, stream=StringIO(), interactive=False))


if __name__ == "__main__":
    unittest.main()
