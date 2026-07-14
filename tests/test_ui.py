"""UI-layer tests: theme, the gui's pure helpers, and the dialogs' row-builders.

The GUI is tested WITHOUT a display — every decision this layer makes lives in a pure function.
The few tests that need a real Tk() call _tk_or_skip(), which skips (never fails) on a headless box.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _tk_or_skip():
    """A withdrawn Tk root, or skip the test on a headless machine (CI has no display)."""
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as e:                       # noqa: BLE001 — TclError, ImportError, anything
        raise unittest.SkipTest(f"no Tk display: {e}")
    root.withdraw()
    return root


class TestPickFont(unittest.TestCase):
    def test_prefers_the_first_available_family(self):
        from cas.theme import pick_font
        self.assertEqual(pick_font(["DejaVu Sans", "Inter", "Comic Sans MS"]), "Inter")

    def test_is_case_insensitive(self):
        from cas.theme import pick_font
        self.assertEqual(pick_font(["segoe ui"]), "Segoe UI")

    def test_falls_back_when_nothing_matches(self):
        from cas.theme import pick_font
        self.assertEqual(pick_font(["Wingdings"]), "TkDefaultFont")

    def test_honours_a_custom_preference_list(self):
        from cas.theme import pick_font
        self.assertEqual(pick_font(["Consolas"], prefs=("Consolas",), fallback="TkFixedFont"),
                         "Consolas")


class TestPalette(unittest.TestCase):
    def test_light_palette_has_every_key_the_styles_use(self):
        from cas.theme import LIGHT
        for k in ("surface", "surface_alt", "border", "text", "muted", "accent",
                  "accent_hover", "accent_tint", "ok", "warn", "danger"):
            self.assertIn(k, LIGHT)
            self.assertTrue(LIGHT[k].startswith("#"), f"{k} must be a hex colour")

    def test_accent_is_the_gamecove_purple(self):
        from cas.theme import LIGHT
        self.assertEqual(LIGHT["accent"].upper(), "#A855F7")


class TestApplyTheme(unittest.TestCase):
    def test_apply_configures_the_accent_button_and_taller_rows(self):
        from tkinter import ttk
        from cas import theme
        root = _tk_or_skip()
        try:
            palette, fonts = theme.apply(root)
            st = ttk.Style(root)
            self.assertEqual(st.theme_use(), "clam")
            self.assertEqual(st.lookup("Accent.TButton", "background"), palette["accent"])
            self.assertEqual(int(st.lookup("Treeview", "rowheight")), theme.ROW_HEIGHT)
            self.assertIn("title", fonts)
        finally:
            root.destroy()
