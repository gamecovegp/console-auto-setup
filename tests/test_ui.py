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


class TestSelectionSummary(unittest.TestCase):
    def test_no_devices_connected(self):
        from cas.gui import _selection_summary
        self.assertEqual(_selection_summary(0, 0), "No devices connected")

    def test_none_selected_names_the_total(self):
        from cas.gui import _selection_summary
        self.assertEqual(_selection_summary(0, 5), "No devices selected  ·  5 connected")

    def test_partial_selection(self):
        from cas.gui import _selection_summary
        self.assertEqual(_selection_summary(3, 5), "3 of 5 devices selected")

    def test_everything_selected_is_called_out(self):
        from cas.gui import _selection_summary
        self.assertEqual(_selection_summary(5, 5), "ALL 5 devices selected")


class TestRightClickSelection(unittest.TestCase):
    """The file-manager rule — Tk's Treeview does NOT do this on its own."""
    def test_clicking_outside_the_selection_replaces_it(self):
        from cas.gui import _rightclick_selection
        self.assertEqual(_rightclick_selection("C", ("A", "B")), ("C",))

    def test_clicking_inside_a_multi_selection_keeps_it(self):
        from cas.gui import _rightclick_selection
        self.assertEqual(_rightclick_selection("B", ("A", "B")), ("A", "B"))

    def test_clicking_empty_space_keeps_the_selection(self):
        from cas.gui import _rightclick_selection
        self.assertEqual(_rightclick_selection("", ("A",)), ("A",))

    def test_clicking_empty_space_with_nothing_selected(self):
        from cas.gui import _rightclick_selection
        self.assertEqual(_rightclick_selection("", ()), ())


class TestContextActions(unittest.TestCase):
    def test_save_needs_exactly_one_online_device(self):
        from cas.gui import _context_actions
        self.assertTrue(_context_actions(1, "device")["save"])
        self.assertFalse(_context_actions(2, "device")["save"])
        self.assertFalse(_context_actions(1, "offline")["save"])

    def test_assign_and_run_work_on_any_selection(self):
        from cas.gui import _context_actions
        a = _context_actions(3, "device")
        for k in ("assign_profile", "assign_firmware", "run_root", "run_download",
                  "run_warmup", "run_lock", "copy_serial"):
            self.assertTrue(a[k], k)

    def test_nothing_is_enabled_with_an_empty_selection(self):
        from cas.gui import _context_actions
        self.assertFalse(any(_context_actions(0, "device").values()))

    def test_seal_and_release_are_single_device_only(self):
        from cas.gui import _context_actions
        self.assertTrue(_context_actions(1, "device")["seal"])
        self.assertFalse(_context_actions(2, "device")["seal"])
        self.assertFalse(_context_actions(1, "offline")["release"])   # release needs adb


class TestRowCells(unittest.TestCase):
    def test_a_pinned_profile_is_marked_in_the_cell(self):
        from cas.gui import _profile_cell
        self.assertEqual(_profile_cell("air-x-128", True), "air-x-128  (pinned)")

    def test_an_auto_matched_profile_is_plain(self):
        from cas.gui import _profile_cell
        self.assertEqual(_profile_cell("air-x-128", False), "air-x-128")

    def test_no_match_never_claims_to_be_pinned(self):
        from cas.gui import _profile_cell
        self.assertEqual(_profile_cell("(no match)", True), "(no match)")

    def test_state_cell_carries_a_dot(self):
        from cas.gui import _state_cell
        self.assertEqual(_state_cell("device"), "● device")
        self.assertEqual(_state_cell(""), "● ?")
