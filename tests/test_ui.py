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

    def test_state_colors_values_are_all_light_keys(self):
        """BLOCKING FINDING 5: gui._populate_devices does THEME.LIGHT[key] for every STATE_COLORS value,
        on the UI thread, mid device-list refresh. A value that isn't a LIGHT key is a KeyError that
        empties the whole device list. Locks the coupling so drift is caught here, not on the bench."""
        from cas.theme import STATE_COLORS, LIGHT
        for state, key in STATE_COLORS.items():
            self.assertIn(key, LIGHT, f"STATE_COLORS[{state!r}] -> {key!r} is not a LIGHT key")


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

    def test_seal_needs_an_online_device_too(self):
        """E5: sealing goes over adb — an offline/unauthorized row can only ever fail after a guaranteed
        2-3 min flash-and-wait, so it must be gated exactly like save/release."""
        from cas.gui import _context_actions
        self.assertTrue(_context_actions(1, "device")["seal"])
        self.assertFalse(_context_actions(1, "offline")["seal"])
        self.assertFalse(_context_actions(1, "unauthorized")["seal"])

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
        self.assertFalse(a["seal"], "seal needs adb and should be disabled in fastboot state")


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


class TestContextMenuWiring(unittest.TestCase):
    """The context menu builds from the PURE gating (no Tk). These tests pin the wiring contract the
    menu relies on: assign_* take their target explicitly now, so a menu item can act on a selection
    the combobox never knew about."""

    def _app(self):
        from cas.gui import App
        app = App.__new__(App)                             # bypass Tk __init__ (no display in CI)
        app.assigned = {"S1": "p1", "S2": "p2"}
        app.assigned_manual = set()
        app.profiles_root = "."
        app.log = lambda m: None
        app.refresh_devices = lambda: None
        return app

    def test_assign_profile_takes_the_name_and_the_serials(self):
        import inspect
        from cas.gui import App
        sig = inspect.signature(App.assign_profile)
        self.assertEqual(list(sig.parameters)[1:3], ["name", "serials"])

    def test_assign_firmware_takes_the_id_and_the_serials(self):
        import inspect
        from cas.gui import App
        sig = inspect.signature(App.assign_firmware)
        self.assertEqual(list(sig.parameters)[1:3], ["fid", "serials"])

    def test_unassign_profile_clears_the_manual_override(self):
        from unittest import mock
        app = self._app()
        app.assigned_manual.add("S1")
        with mock.patch("cas.gui.config.set_device_profile") as sdp, \
             mock.patch("cas.gui.messagebox.askyesno", return_value=True):
            app.unassign_profile(["S1"])
        sdp.assert_called_once_with("S1", None)
        self.assertNotIn("S1", app.assigned_manual)


class _FakeCtxTree:
    """Stand-in for the real Treeview, just enough for App._popup_context: identify_row maps a y-coord
    to a row id (empty string = empty space, matching the real Treeview), selection()/selection_set()/
    focus() model Tk's actual semantics (selection_set REPLACES the whole selection) — and every mutating
    call is recorded so a test can assert exactly what _popup_context told the tree to do."""

    def __init__(self, row_at_y, initial_selection=()):
        self._row_at_y = row_at_y
        self._selection = tuple(initial_selection)
        self._focus = None
        self.selection_set_calls = []
        self.focus_calls = []

    def identify_row(self, y):
        return self._row_at_y.get(y, "")

    def selection(self):
        return self._selection

    def selection_set(self, items):
        items = tuple(items)
        self._selection = items
        self.selection_set_calls.append(items)

    def focus(self, item=None):
        if item is None:
            return self._focus
        self._focus = item
        self.focus_calls.append(item)


class TestPopupContext(unittest.TestCase):
    """BLOCKING FINDING 2 regression: App._popup_context is the CALL SITE that applies the pure
    _rightclick_selection() rule to the real tree. The pure rule already had coverage, but the wiring
    that actually calls it (selection_set + which serials the menu gets built for) had NONE — a reviewer
    mutant that built the menu from the STALE selection (dropping selection_set entirely) sailed through
    the full 466-test suite. That mutant fires an action on devices the operator never clicked — the
    brick scenario. These tests pin the real Treeview call site, not just the pure helper."""

    def _app(self, row_at_y, initial_selection=()):
        from cas.gui import App
        app = App.__new__(App)                              # bypass Tk __init__ (no display in CI)
        app.dev_tree = _FakeCtxTree(row_at_y, initial_selection)
        app.ctx = "CTX_MENU"                                # sentinel objects — identity-compared below
        app.ctx_empty = "CTX_EMPTY_MENU"
        app._posted = []
        app._post_menu = lambda menu, event: app._posted.append(menu)
        app._built_for = []
        app._rebuild_context_menu = lambda serials: app._built_for.append(list(serials))
        return app

    def _event(self, y):
        import types
        return types.SimpleNamespace(y=y, x_root=0, y_root=0)

    def test_rightclick_outside_the_selection_narrows_to_the_clicked_row(self):
        app = self._app(row_at_y={10: "C"}, initial_selection=("A", "B"))
        app._popup_context(self._event(10))
        self.assertEqual(app.dev_tree.selection_set_calls, [("C",)])
        self.assertEqual(app.dev_tree.focus_calls, ["C"])
        self.assertEqual(app._built_for, [["C"]])
        self.assertEqual(app._posted, [app.ctx])

    def test_rightclick_inside_a_multiselection_preserves_it(self):
        app = self._app(row_at_y={10: "B"}, initial_selection=("A", "B"))
        app._popup_context(self._event(10))
        self.assertEqual(app.dev_tree.selection_set_calls, [("A", "B")])
        self.assertEqual(app._built_for, [["A", "B"]])
        self.assertEqual(app._posted, [app.ctx])

    def test_rightclick_empty_space_with_nothing_selected_posts_the_empty_menu(self):
        app = self._app(row_at_y={}, initial_selection=())
        app._popup_context(self._event(999))                # no row at y=999 -> empty space
        self.assertEqual(app.dev_tree.selection_set_calls, [])   # selection NEVER touched
        self.assertEqual(app.dev_tree.focus_calls, [])
        self.assertEqual(app._built_for, [])                     # the real menu is never rebuilt
        self.assertEqual(app._posted, [app.ctx_empty])

    def test_rightclick_empty_space_with_an_existing_selection_leaves_it_alone(self):
        """_rightclick_selection's rule: empty space never CLEARS a selection — it's a no-op on whatever
        was already selected, so the real menu (not the empty one) is built for it."""
        app = self._app(row_at_y={}, initial_selection=("A", "B"))
        app._popup_context(self._event(999))
        self.assertEqual(app.dev_tree.selection_set_calls, [("A", "B")])
        self.assertEqual(app._built_for, [["A", "B"]])
        self.assertEqual(app._posted, [app.ctx])


class _FakeDevTree:
    """Just enough of a ttk.Treeview for assign_profile: item(serial) reads the row's `values`
    (values[0] is the model column), item(serial, values=...) rewrites it, exists() checks presence."""

    def __init__(self, rows):
        # rows: {serial: model} -> a 5-column row like the real tree (model, ..., profile-cell, ..., ...)
        self._rows = {s: [m, "", "", "", ""] for s, m in rows.items()}

    def item(self, serial, values=None):
        if values is not None:
            self._rows[serial] = list(values)
            return None
        return {"values": list(self._rows.get(serial, []))}

    def exists(self, serial):
        return serial in self._rows


class TestAssignProfileModelMismatchGuard(unittest.TestCase):
    """assign_profile is the SAFETY GATE before Root/Lock flash a profile's Magisk-patched init_boot
    onto a device: when the profile's model_match doesn't fit the selected device, it must warn the
    operator (naming the device, warning about flashing/bootloop) and only proceed on confirmation.
    CAS has bricked a real device by flashing a kernel-less image to the wrong unit — this is the guard
    that prevents a repeat, and it had NEVER had automated coverage before this class."""

    MISMATCH_MODEL_MATCH = "AIR ?X"      # regex the profile targets
    MISMATCH_DEVICE_MODEL = "Odin2 Mini"  # a device model this regex does NOT match
    MATCH_DEVICE_MODEL = "AIR X"          # a device model this regex DOES match

    def _make_profile(self, profiles_root, name, model_match):
        d = pathlib.Path(profiles_root) / name
        d.mkdir(parents=True)
        # A REAL profile.meta on disk — profiles.Profile(...).meta must read it for real so the
        # re.search(model_match, model) path actually executes (not mocked away).
        (d / "profile.meta").write_text(f"model_match={model_match}\n")
        return d

    def _app(self, profiles_root, dev_rows):
        from cas.gui import App
        app = App.__new__(App)                            # bypass Tk __init__ (no display in CI)
        app.assigned = {}
        app.assigned_manual = set()
        app.profiles_root = str(profiles_root)
        app.log = lambda m: None
        app.dev_tree = _FakeDevTree(dev_rows)
        return app

    def test_mismatch_warns_with_device_and_bootloop_text(self):
        with tempfile.TemporaryDirectory() as t:
            self._make_profile(t, "p1", self.MISMATCH_MODEL_MATCH)
            app = self._app(t, {"SER1": self.MISMATCH_DEVICE_MODEL})
            with mock.patch("cas.gui.messagebox.askyesno", return_value=False) as askyesno, \
                 mock.patch("cas.gui.config.set_device_profile") as sdp:
                app.assign_profile("p1", ["SER1"])
            askyesno.assert_called_once()
            (_title, text), _kwargs = askyesno.call_args
            # the mismatched device is named (serial + the model that failed to match)...
            self.assertIn("SER1", text)
            self.assertIn(self.MISMATCH_DEVICE_MODEL, text)
            # ...and the warning describes the real danger: it FLASHES and can bootloop.
            self.assertIn("FLASH", text)
            self.assertIn("bootloop", text)
            sdp.assert_not_called()          # declined below, but this asserts the warning content only

    def test_declining_the_mismatch_warning_assigns_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            self._make_profile(t, "p1", self.MISMATCH_MODEL_MATCH)
            app = self._app(t, {"SER1": self.MISMATCH_DEVICE_MODEL})
            app.assigned = {"SER1": "old-profile"}          # a pre-existing state that must survive
            with mock.patch("cas.gui.messagebox.askyesno", return_value=False), \
                 mock.patch("cas.gui.config.set_device_profile") as sdp:
                app.assign_profile("p1", ["SER1"])
            # nothing changed: assigned map untouched, no manual-pin added, no persistence call made
            self.assertEqual(app.assigned, {"SER1": "old-profile"})
            self.assertNotIn("SER1", app.assigned_manual)
            sdp.assert_not_called()

    def test_accepting_the_mismatch_warning_persists_the_assignment(self):
        with tempfile.TemporaryDirectory() as t:
            self._make_profile(t, "p1", self.MISMATCH_MODEL_MATCH)
            app = self._app(t, {"SER1": self.MISMATCH_DEVICE_MODEL})
            with mock.patch("cas.gui.messagebox.askyesno", return_value=True), \
                 mock.patch("cas.gui.config.set_device_profile") as sdp:
                app.assign_profile("p1", ["SER1"])
            self.assertEqual(app.assigned.get("SER1"), "p1")
            self.assertIn("SER1", app.assigned_manual)
            sdp.assert_called_once_with("SER1", "p1", manual=True)

    def test_matching_model_does_not_raise_the_bootloop_warning(self):
        with tempfile.TemporaryDirectory() as t:
            self._make_profile(t, "p1", self.MISMATCH_MODEL_MATCH)
            app = self._app(t, {"SER1": self.MATCH_DEVICE_MODEL})
            with mock.patch("cas.gui.messagebox.askyesno", return_value=True) as askyesno, \
                 mock.patch("cas.gui.config.set_device_profile"):
                app.assign_profile("p1", ["SER1"])
            askyesno.assert_called_once()
            (_title, text), _kwargs = askyesno.call_args
            # a matching model still asks for a plain confirm, but MUST NOT carry the mismatch warning
            self.assertNotIn("bootloop", text)
            self.assertNotIn("FLASH", text)
            self.assertIn("p1", text)                        # still a real (plain) confirmation prompt


class TestActionTargets(unittest.TestCase):
    """▶ Run targets the SELECTION, always. The 'Apply to ALL connected devices' toggle is gone — it
    silently redefined what the main button did."""

    def _app(self, selection):
        from cas.gui import App
        import types
        app = App.__new__(App)
        app.dev_tree = types.SimpleNamespace(selection=lambda: list(selection),
                                             get_children=lambda: ["S1", "S2", "S3"])
        return app

    def test_targets_are_exactly_the_selected_rows(self):
        app = self._app(["S1", "S3"])
        self.assertEqual(app._action_targets(), ["S1", "S3"])

    def test_an_empty_selection_targets_nothing(self):
        from unittest import mock
        app = self._app([])
        with mock.patch("cas.gui.messagebox.showinfo") as info:
            self.assertIsNone(app._action_targets())
        info.assert_called_once()

    def test_the_batch_toggle_is_gone(self):
        from cas.gui import App
        self.assertFalse(hasattr(App, "_on_batch_toggle"),
                         "the Apply-to-ALL toggle and its handler must be removed")


class _FakeVar:
    """Stands in for a tk.BooleanVar — just get()/set() over a plain attribute."""
    def __init__(self, value=False):
        self.value = value

    def get(self):
        return self.value

    def set(self, v):
        self.value = v


class _FakeCheckbutton:
    """Stands in for a ttk.Checkbutton — records the last state configure() set."""
    def __init__(self):
        self.state = "normal"

    def configure(self, state=None, **kw):
        if state is not None:
            self.state = state


class TestSaveSelectionGate(unittest.TestCase):
    """BLOCKING FINDING 1 (the proactive half of the fix): the ① Save checkbox itself must reflect
    'exactly one device selected' — not just refuse at ▶ Run time — so the operator sees the constraint
    BEFORE ticking it, the same way Download/Warm up/Lock already grey out while Save is ticked."""

    def _app(self, selection, save_ticked=False, unit_ticked=False):
        from cas.gui import App
        import types
        app = App.__new__(App)
        app.dev_tree = types.SimpleNamespace(selection=lambda: list(selection))
        app.chain_vars = {"save": _FakeVar(save_ticked), "download": _FakeVar(unit_ticked),
                          "warmup": _FakeVar(False), "lock": _FakeVar(False)}
        app.chain_cbs = {"save": _FakeCheckbutton()}
        return app

    def test_disabled_and_unticked_when_selection_is_not_exactly_one(self):
        app = self._app(["S1", "S2"], save_ticked=True)
        app._sync_save_selection_gate()
        self.assertEqual(app.chain_cbs["save"].state, "disabled")
        self.assertFalse(app.chain_vars["save"].get())

    def test_disabled_when_nothing_selected(self):
        app = self._app([], save_ticked=True)
        app._sync_save_selection_gate()
        self.assertEqual(app.chain_cbs["save"].state, "disabled")
        self.assertFalse(app.chain_vars["save"].get())

    def test_enabled_when_exactly_one_selected(self):
        app = self._app(["S1"])
        app._sync_save_selection_gate()
        self.assertEqual(app.chain_cbs["save"].state, "normal")

    def test_leaves_a_unit_chain_disabled_gate_alone(self):
        """_on_chain_tick already disabled Save because Download/Warm up/Lock is ticked — the selection
        gate must defer to that (early-return), not fight it."""
        app = self._app(["S1"], unit_ticked=True)
        app.chain_cbs["save"].state = "disabled"      # what _on_chain_tick would have already set
        app._sync_save_selection_gate()
        self.assertEqual(app.chain_cbs["save"].state, "disabled")


class TestMainAppliesTheTheme(unittest.TestCase):
    # test_main_calls_theme_apply (inspect.getsource text-matching) removed — E6: it was redundant with
    # TestGuiMainOrdering below, which is a real behavioural test of the same ordering guarantee and is
    # already proven non-vacuous.

    def test_a_themed_root_reaches_the_built_widgets(self):
        """Runtime check (guarded — skips headless): applying the theme directly to a root and THEN
        building the REAL App on it, confirms the styling actually lands on a real widget (not just that
        some code calls apply() textually). This does NOT exercise gui.main()'s own ordering — it applies
        the theme and builds App itself, so it stays green even if main() stopped theming altogether or
        themed too late. See TestGuiMainOrdering below for the behavioural test of main()'s ordering."""
        root = _tk_or_skip()
        try:
            from tkinter import ttk
            from cas import theme
            from cas.gui import App
            palette, _fonts = theme.apply(root)
            # Bogus binaries: refresh_devices() spawns a background thread that shells out to
            # list_devices(); a nonexistent path fails fast (FileNotFoundError, caught + logged) instead
            # of racing root.destroy() below against a real `adb devices` subprocess call.
            app = App(root, adb_bin="/nonexistent/adb-for-tests", fb_bin="/nonexistent/fastboot-for-tests")
            st = ttk.Style(root)
            self.assertEqual(st.lookup("Accent.TButton", "background"), palette["accent"])
            self.assertEqual(str(app.run_btn.cget("style")), "Accent.TButton")
        finally:
            root.destroy()


class TestGuiMainOrdering(unittest.TestCase):
    """Behavioural test of gui.main() itself (not a stand-in that rebuilds the same steps): proves
    main() applies the theme to the SAME root it hands to App, and does so BEFORE App is built — a
    themed-too-late or theme-skipped main() must fail this test. No real Tk/display is created; tk.Tk,
    THEME.apply and App are all replaced with recorders so this runs headless."""

    def test_theme_is_applied_before_app_is_built_on_the_same_root(self):
        from cas import gui

        calls = []
        fake_root = mock.MagicMock(name="fake_root")

        def fake_apply(win):
            calls.append(("apply", win))
            return ({"accent": "#123456"}, {})

        def fake_app(win, **kwargs):
            calls.append(("App", win))
            return mock.MagicMock(name="fake_app")

        with mock.patch.object(gui.tk, "Tk", return_value=fake_root), \
             mock.patch.object(gui.THEME, "apply", side_effect=fake_apply), \
             mock.patch.object(gui, "App", side_effect=fake_app):
            gui.main(adb_bin="adb-for-tests", fb_bin="fastboot-for-tests")

        self.assertEqual([name for name, _win in calls], ["apply", "App"],
                          "gui.main() must call THEME.apply(win) before App(win, ...)")
        self.assertIs(calls[0][1], fake_root, "THEME.apply must be given the root main() creates")
        self.assertIs(calls[1][1], fake_root, "App must be built on that SAME root")
        fake_root.mainloop.assert_called_once()
