"""UI-layer tests: theme, the gui's pure helpers, and the dialogs' row-builders.

The GUI is tested WITHOUT a display — every decision this layer makes lives in a pure function.
The few tests that need a real Tk() call _tk_or_skip(), which skips (never fails) on a headless box.
"""
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_release_is_enabled_for_one_online_device(self):
        """COVERAGE FINDING 1: release's enabled case was never asserted."""
        from cas.gui import _context_actions
        self.assertTrue(_context_actions(1, "device")["release"])

    def test_offline_tolerant_actions_work_in_fastboot_state(self):
        """COVERAGE FINDING 2: offline-tolerant actions (Root, Lock, firmware/profile assign)
        must work in fastboot/EDL states, but save and release need adb and must be disabled."""
        from cas.gui import _context_actions
        a = _context_actions(1, "fastboot")
        # These actions do NOT need adb and should be enabled offline
        for k in ("assign_profile", "assign_firmware", "run_root", "run_download",
                  "run_warmup", "run_lock", "copy_serial"):
            self.assertTrue(a[k], f"{k} should be enabled in fastboot state")
        # These actions NEED adb and should be disabled offline
        self.assertFalse(a["save"], "save needs adb and should be disabled in fastboot state")
        self.assertFalse(a["release"], "release needs adb and should be disabled in fastboot state")


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


class TestHumanSize(unittest.TestCase):
    def test_zero_bytes_is_an_em_dash(self):
        from cas.dialogs import human_size
        self.assertEqual(human_size(0), "—")

    def test_mb_scale_value(self):
        from cas.dialogs import human_size
        self.assertEqual(human_size(5 * 1024 * 1024), "5.0 MB")

    def test_gb_scale_value(self):
        from cas.dialogs import human_size
        self.assertEqual(human_size(3 * 1024 * 1024 * 1024), "3.0 GB")


class TestProfileRows(unittest.TestCase):
    def _library(self, td):
        """A profile library with one golden-bearing profile and one empty one."""
        root = pathlib.Path(td)
        a = root / "air-x-128"
        (a / "golden_root_payload").mkdir(parents=True)
        (a / "profile.meta").write_text("model_match=AIR X\ncaptured=2026-07-11\n")
        (a / "golden_root_payload" / "global.meta").write_text("x=1\n")   # has_golden() looks for this
        b = root / "odin2-mini"
        b.mkdir(parents=True)
        (b / "profile.meta").write_text("model_match=\ncaptured=\n")
        return str(root)

    def test_rows_report_the_golden_and_the_model(self):
        from cas.dialogs import profile_rows
        with tempfile.TemporaryDirectory() as td:
            rows = profile_rows(self._library(td))
        self.assertEqual([r["name"] for r in rows], ["air-x-128", "odin2-mini"])
        self.assertTrue(rows[0]["has_golden"])
        self.assertEqual(rows[0]["model"], "AIR X")
        self.assertEqual(rows[0]["captured"], "2026-07-11")
        self.assertFalse(rows[1]["has_golden"])

    def test_a_missing_library_yields_no_rows(self):
        from cas.dialogs import profile_rows
        self.assertEqual(profile_rows("/nonexistent/library"), [])


class TestOverwriteWarning(unittest.TestCase):
    def test_a_profile_with_a_golden_warns_that_save_replaces_it(self):
        from cas.dialogs import overwrite_warning
        msg = overwrite_warning({"name": "air-x-128", "has_golden": True})
        self.assertIn("air-x-128", msg)
        self.assertIn("REPLACE", msg.upper())

    def test_an_empty_profile_does_not_warn(self):
        from cas.dialogs import overwrite_warning
        self.assertEqual(overwrite_warning({"name": "odin2-mini", "has_golden": False}), "")


class TestFirmwareRows(unittest.TestCase):
    def _library(self, td):
        """A firmware library shaped the way cas.firmware.ingest() writes it: <root>/<id>/meta.json
        (not firmware.json — meta.json is what cas.firmware.Firmware.__init__ actually reads) plus a
        versions/<version>/payload/ tree."""
        import json
        root = pathlib.Path(td) / "_firmware"
        fw = root / "mangmi-air-x-mq66"
        (fw / "versions" / "v1" / "payload").mkdir(parents=True)
        (fw / "meta.json").write_text(json.dumps({
            "id": "mangmi-air-x-mq66", "device": "AIR X", "storage": "emmc",
            "flash_target": "init_boot", "current": "v1",
            "match": {"serial_prefix": ["MQ66"]},
        }))
        return root

    def test_rows_report_id_version_and_match(self):
        from cas.dialogs import firmware_rows
        with tempfile.TemporaryDirectory() as td:
            rows = firmware_rows(self._library(td))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "mangmi-air-x-mq66")
        self.assertEqual(rows[0]["version"], "v1")
        self.assertIn("MQ66", rows[0]["match"])
        self.assertEqual(rows[0]["device"], "AIR X")
        self.assertEqual(rows[0]["target"], "init_boot")

    def test_an_empty_library_yields_no_rows(self):
        from cas.dialogs import firmware_rows
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(firmware_rows(pathlib.Path(td)), [])


class _FakeWin:
    """Stands in for the Tk root in App.__new__(App) tests. Records every after() call and — since
    there's no real Tk mainloop here — invokes the callback immediately, the same way a real
    `after(0, fn)` fires once the event loop next ticks."""

    def __init__(self):
        self.after_calls = []          # list of (delay, fn)

    def after(self, delay, fn=None):
        self.after_calls.append((delay, fn))
        if fn is not None:
            fn()


class TestAddFirmwareOnDone(unittest.TestCase):
    """Important-finding regression: FirmwareWindow._add() used to guess completion with a fixed
    `self.win.after(1500, self.refresh)` after kicking off App._add_firmware()'s background ingest —
    a multi-GB shutil.copytree that is virtually never done at 1.5s, so the window refreshed to
    nothing new and never refreshed again. The fix threads an `on_done` callback through
    App._add_firmware() that fires on the UI thread only once the ingest's work() closure — which
    already signals completion via `self.win.after(0, ...)` — actually finishes."""

    class _FakeFw:
        id = "mangmi-air-x-mq66"

        def current(self):
            return "v1"

        def match_rules(self):
            return {"serial_prefix": ["MQ66"]}

    def _app(self):
        import cas.gui as G
        app = G.App.__new__(G.App)             # bypass Tk __init__
        app.win = _FakeWin()
        app.log = lambda m: None
        app.refresh_firmware = lambda: None
        app.refresh_devices = lambda: None
        app._bg_calls = []

        def fake_run_bg(fn, label="Working"):
            app._bg_calls.append(label)
            fn()                                # run the work() closure synchronously, like the real
        app._run_bg = fake_run_bg               # background thread would, minus the threading
        return app

    def test_on_done_fires_after_ingest_completes_not_on_a_timer(self):
        import cas.gui as G
        app = self._app()
        events = []

        def fake_ingest(*a, **kw):
            events.append("ingest")             # the (stand-in for a) multi-GB copy happening
            return self._FakeFw()

        def on_done():
            events.append("on_done")

        with mock.patch.object(G.filedialog, "askdirectory", return_value="/tmp/build"), \
             mock.patch.object(G.simpledialog, "askstring", side_effect=["mangmi-air-x-mq66", ""]), \
             mock.patch.object(G.FW, "ingest", side_effect=fake_ingest):
            app._add_firmware(on_done=on_done)

        # completion is signalled AFTER the ingest actually ran — never guessed ahead of it
        self.assertEqual(events, ["ingest", "on_done"])
        # every after() call along this path is a completion signal (delay 0), never a guessed
        # fixed delay like the old `after(1500, self.refresh)`
        delays = [d for d, _fn in app.win.after_calls]
        self.assertTrue(delays, "expected at least one after() call")
        self.assertNotIn(1500, delays)
        self.assertTrue(all(d == 0 for d in delays), f"expected only delay=0 after() calls, got {delays}")

    def test_on_done_is_optional_and_backward_compatible(self):
        import cas.gui as G
        app = self._app()
        with mock.patch.object(G.filedialog, "askdirectory", return_value="/tmp/build"), \
             mock.patch.object(G.simpledialog, "askstring", side_effect=["x", ""]), \
             mock.patch.object(G.FW, "ingest", return_value=self._FakeFw()):
            app._add_firmware()                 # no on_done passed — must not raise
        self.assertEqual(app._bg_calls, ["Ingesting firmware"])


class TestFirmwareWindowAddWiring(unittest.TestCase):
    """FirmwareWindow._add() must hand App._add_firmware() a real completion callback (not a fixed
    delay), and that callback must be safe if the operator closed the Firmware window while the
    ingest was still running (tk.TclError from a destroyed widget)."""

    def _win(self):
        import cas.dialogs as D
        return D.FirmwareWindow.__new__(D.FirmwareWindow)          # bypass Tk __init__

    def test_add_passes_on_ingest_done_as_the_callback(self):
        win = self._win()
        captured = {}

        def fake_add_firmware(on_done=None):
            captured["on_done"] = on_done
        win.app = mock.Mock(_add_firmware=fake_add_firmware)

        win._add()

        # bound methods aren't `is`-identical across accesses; compare by equality (same instance+func)
        self.assertEqual(captured.get("on_done"), win._on_ingest_done)

    def test_on_ingest_done_refreshes_the_list(self):
        win = self._win()
        calls = []
        win.refresh = lambda: calls.append(1)

        win._on_ingest_done()

        self.assertEqual(calls, [1])

    def test_on_ingest_done_is_safe_if_the_window_was_closed_mid_ingest(self):
        import tkinter as tk
        win = self._win()

        def boom():
            raise tk.TclError("window closed")
        win.refresh = boom

        win._on_ingest_done()                   # must not raise


class TestProfilesWindowOnSelectClosedMidWalk(unittest.TestCase):
    """Minor: ProfilesWindow._on_select()'s worker thread marshals its result back with
    `self.win.after(0, ...)`; if the operator closes the Profiles window mid-walk, updating a
    destroyed StringVar raises tk.TclError. `_set_detail` must swallow that quietly, matching
    ProfilePicker._set_size's pattern."""

    def test_set_detail_is_safe_if_the_window_was_closed(self):
        import tkinter as tk
        import cas.dialogs as D
        win = D.ProfilesWindow.__new__(D.ProfilesWindow)

        class _BoomVar:
            def set(self, v):
                raise tk.TclError("window closed")
        win.detail_var = _BoomVar()

        win._set_detail("some-profile", 12345, " · ~1m to download")   # must not raise

    def test_set_detail_updates_the_label_when_the_window_is_still_open(self):
        import cas.dialogs as D
        win = D.ProfilesWindow.__new__(D.ProfilesWindow)

        class _Var:
            def __init__(self):
                self.value = None

            def set(self, v):
                self.value = v
        win.detail_var = _Var()

        win._set_detail("some-profile", 12345, " · ~1m to download")

        self.assertIn("some-profile", win.detail_var.value)
        self.assertIn("~1m to download", win.detail_var.value)
