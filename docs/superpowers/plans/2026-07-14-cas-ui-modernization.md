# CAS UI/UX Modernization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the CAS main window around a full-width device list with right-click per-device actions, replace the Save name-prompt with a profile picker, and give the whole app a modern flat look — with zero new dependencies.

**Architecture:** Two new modules — `cas/theme.py` (a pure-stdlib restyle of ttk's `clam` theme) and `cas/dialogs.py` (the profile picker plus the Profiles / Firmware / box-art windows that absorb the deleted right panel). `cas/gui.py` keeps `App` but loses its right column and its "Apply to ALL" toggle: the device Treeview goes full width, a toolbar opens the library windows, and a right-click context menu drives per-device assignment and single-step runs. All new decision logic lives in pure module-level functions so it unit-tests without a display, matching how the GUI is tested today.

**Tech Stack:** Python 3.13, Tkinter/ttk (stdlib only), `unittest` (stdlib), PyInstaller for packaging.

**Spec:** `docs/superpowers/specs/2026-07-14-cas-ui-modernization-design.md`

## Global Constraints

- **No third-party runtime dependency.** CAS ships none today (Pillow is optional and import-guarded inside `try/except`). Do not add one. Do not import Pillow at module scope.
- **Tests are stdlib `unittest`, run from the `tests/` directory:** `cd tests && python -m unittest discover -p "test_*.py"`. Baseline before this plan: **411 tests, OK**. Every task must leave the suite green.
- **No Tk display in CI.** New logic goes in pure functions. Any test that needs a real `Tk()` must skip cleanly when no display is available (helper provided in Task 1).
- **No config-format change.** `device_profiles`, firmware overrides, `es_media_src`, `always_install` keep their current keys and semantics.
- **Windows-consumed scripts must stay pure ASCII** (existing guard test). This does not apply to `cas/*.py`, which is UTF-8 and already uses glyphs like `⓪ ▶ ✗`.
- **Accent colour is `#A855F7`** (the GameCove purple already in `gui._PH_PALETTE`).
- Commit messages end with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

| File | Responsibility |
|---|---|
| `cas/theme.py` | **new.** Palette dict, UI-font resolution, `apply(root)` → restyles ttk `clam` in place. No CAS imports — standalone. |
| `cas/dialogs.py` | **new.** Every window that is *not* the main window: `ProfilePicker` (Save step 1 / assign), `ProfilesWindow`, `FirmwareWindow`, `BoxArtDialog`. Owns the pure row-builders they render. |
| `cas/gui.py` | **modified.** `App` = toolbar + full-width device list + context menu + chain footer + log. Right panel, notebook, batch toggle and their handlers deleted. |
| `tests/test_ui.py` | **new.** Pure-function tests for `theme` + the new `gui` helpers + the `dialogs` row-builders, plus display-guarded smoke tests. |
| `tests/test_cas.py` | **modified.** Two existing tests call `assign_firmware()` through a stubbed `fw_var`; they pass the id as an argument now. |

---

### Task 1: `cas/theme.py` — palette, fonts, ttk restyle

**Files:**
- Create: `cas/theme.py`
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: nothing (standalone module).
- Produces:
  - `theme.LIGHT` → `dict[str, str]` palette (keys: `surface`, `surface_alt`, `border`, `text`, `muted`, `accent`, `accent_hover`, `accent_tint`, `ok`, `warn`, `danger`).
  - `theme.pick_font(available, prefs=FONT_PREFS, fallback="TkDefaultFont") -> str`
  - `theme.apply(root, palette=None) -> (palette_dict, fonts_dict)` — `fonts_dict` keys: `title`, `body`, `caption`, `mono`.
  - `theme.ROW_HEIGHT` → `int` (28), `theme.STATE_COLORS` → `dict[str, str]` mapping an adb state to a palette colour.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ui.py`:

```python
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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd tests && python -m unittest test_ui -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cas.theme'`

- [ ] **Step 3: Write `cas/theme.py`**

```python
"""Flat, modern ttk styling for CAS — pure stdlib (CAS ships NO third-party runtime deps).

apply(root) restyles ttk's 'clam' theme in place: flat surfaces, one accent colour, an 8px spacing
grid, a real type scale and a taller Treeview. What reads as 'old' in a default Tk app is the
beveled 3D relief, the cramped rows and the single font size — not the corner radius Tk can't draw.

Importing this module is display-safe: Tk is only touched inside apply().
"""
from tkinter import font as tkfont, ttk
import tkinter as tk

# One accent (the GameCove purple, already used by the app-icon placeholders), near-white surfaces,
# semantic colours for device state. A dark palette can be added later with the same keys — every
# style below reads the dict, never a literal.
LIGHT = {
    "surface":      "#FFFFFF",
    "surface_alt":  "#F5F6F8",
    "border":       "#E5E7EB",
    "text":         "#1F2024",
    "muted":        "#6B7280",
    "accent":       "#A855F7",
    "accent_hover": "#9333EA",
    "accent_tint":  "#EDE4FB",
    "ok":           "#10B981",
    "warn":         "#F59E0B",
    "danger":       "#EF4444",
}

# Row foreground per adb state — the row itself carries the signal, so a bad unit is visible at a
# glance without hunting the state column.
STATE_COLORS = {
    "device":       "text",
    "unauthorized": "warn",
    "offline":      "danger",
    "fastboot":     "accent",
    "recovery":     "accent",
    "sideload":     "accent",
}

STATE_DOT = "●"
ROW_HEIGHT = 28
ZEBRA = "#FAFAFB"        # the odd-row tint (barely there — enough to track a row across the width)

FONT_PREFS = ("Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text",
              "Helvetica Neue", "Cantarell", "Ubuntu", "DejaVu Sans")
MONO_PREFS = ("Cascadia Mono", "Consolas", "SF Mono", "JetBrains Mono",
              "DejaVu Sans Mono", "Liberation Mono", "Courier New")


def pick_font(available, prefs=FONT_PREFS, fallback="TkDefaultFont"):
    """First family in `prefs` that the system actually has (case-insensitive), else `fallback`.
    Pure — takes the family list instead of asking Tk, so it unit-tests without a display."""
    have = {str(f).lower() for f in available}
    for fam in prefs:
        if fam.lower() in have:
            return fam
    return fallback


def apply(root, palette=None):
    """Restyle ttk on `root`. Returns (palette, fonts). Fonts are kept on `root` so Tk can't GC them."""
    p = dict(palette or LIGHT)
    fams = tkfont.families(root)
    ui = pick_font(fams)
    mono = pick_font(fams, MONO_PREFS, fallback="TkFixedFont")

    base = tkfont.nametofont("TkDefaultFont")
    if ui != "TkDefaultFont":
        base.configure(family=ui, size=10)
    family = base.cget("family")
    fonts = {
        "title":   tkfont.Font(root=root, family=family, size=13, weight="bold"),
        "body":    base,
        "caption": tkfont.Font(root=root, family=family, size=9),
        "mono":    tkfont.Font(root=root, family=(mono if mono != "TkFixedFont" else family), size=9),
    }

    st = ttk.Style(root)
    try:
        st.theme_use("clam")               # clam is the only built-in theme that takes a full restyle
    except tk.TclError:
        pass
    root.configure(background=p["surface_alt"])

    st.configure(".", background=p["surface_alt"], foreground=p["text"],
                 fieldbackground=p["surface"], bordercolor=p["border"],
                 lightcolor=p["surface_alt"], darkcolor=p["surface_alt"],
                 focuscolor=p["accent"], font=base)

    st.configure("TFrame", background=p["surface_alt"])
    st.configure("Card.TFrame", background=p["surface"])
    st.configure("Toolbar.TFrame", background=p["surface"])

    st.configure("TLabel", background=p["surface_alt"], foreground=p["text"])
    st.configure("Muted.TLabel", background=p["surface_alt"], foreground=p["muted"], font=fonts["caption"])
    st.configure("Title.TLabel", background=p["surface_alt"], font=fonts["title"])
    st.configure("Toolbar.TLabel", background=p["surface"], foreground=p["muted"], font=fonts["caption"])
    st.configure("Warn.TLabel", background=p["surface_alt"], foreground=p["warn"])

    # Buttons: flat, generous padding, a state layer on hover/press instead of a 3D bevel.
    st.configure("TButton", background=p["surface"], foreground=p["text"], relief="flat",
                 borderwidth=1, padding=(12, 6), anchor="center")
    st.map("TButton",
           background=[("pressed", p["accent_tint"]), ("active", p["surface_alt"]),
                       ("disabled", p["surface_alt"])],
           bordercolor=[("active", p["accent"]), ("focus", p["accent"])],
           foreground=[("disabled", p["muted"])])
    st.configure("Toolbar.TButton", background=p["surface"], borderwidth=1, padding=(10, 5))
    st.map("Toolbar.TButton",
           background=[("pressed", p["accent_tint"]), ("active", p["surface_alt"])],
           bordercolor=[("active", p["accent"])])
    st.configure("Accent.TButton", background=p["accent"], foreground="#FFFFFF",
                 borderwidth=0, padding=(16, 7))
    st.map("Accent.TButton",
           background=[("pressed", p["accent_hover"]), ("active", p["accent_hover"]),
                       ("disabled", p["border"])],
           foreground=[("disabled", p["muted"])])

    st.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                 foreground=p["text"], rowheight=ROW_HEIGHT, borderwidth=0, relief="flat")
    st.map("Treeview", background=[("selected", p["accent_tint"])],
           foreground=[("selected", p["text"])])
    st.configure("Treeview.Heading", background=p["surface_alt"], foreground=p["muted"],
                 relief="flat", padding=(8, 6), font=fonts["caption"])
    st.map("Treeview.Heading", background=[("active", p["border"])])

    st.configure("TCheckbutton", background=p["surface_alt"], indicatorbackground=p["surface"],
                 indicatorforeground=p["accent"], padding=(2, 4))
    st.map("TCheckbutton", indicatorbackground=[("selected", p["accent"])],
           indicatorforeground=[("selected", "#FFFFFF")])
    st.configure("TRadiobutton", background=p["surface_alt"], indicatorbackground=p["surface"],
                 padding=(2, 4))
    st.map("TRadiobutton", indicatorbackground=[("selected", p["accent"])])

    st.configure("TProgressbar", background=p["accent"], troughcolor=p["border"],
                 borderwidth=0, thickness=6)
    st.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["border"],
                 borderwidth=0, thickness=6)
    st.configure("TScrollbar", background=p["border"], troughcolor=p["surface_alt"],
                 borderwidth=0, arrowsize=12)
    st.configure("TLabelframe", background=p["surface_alt"], bordercolor=p["border"],
                 relief="solid", borderwidth=1)
    st.configure("TLabelframe.Label", background=p["surface_alt"], foreground=p["muted"],
                 font=fonts["caption"])
    st.configure("TEntry", fieldbackground=p["surface"], bordercolor=p["border"], padding=6)
    st.configure("TCombobox", fieldbackground=p["surface"], padding=5)
    st.configure("TNotebook", background=p["surface_alt"], borderwidth=0)
    st.configure("TNotebook.Tab", background=p["surface_alt"], foreground=p["muted"], padding=(14, 7))
    st.map("TNotebook.Tab", background=[("selected", p["surface"])],
           foreground=[("selected", p["text"])])

    root._cas_theme = {"palette": p, "fonts": fonts}     # keep refs: Tk GCs unreferenced Fonts
    return p, fonts
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `cd tests && python -m unittest test_ui -v`
Expected: PASS (the `TestApplyTheme` case passes on a desktop, skips on a headless box).

- [ ] **Step 5: Run the whole suite**

Run: `cd tests && python -m unittest discover -p "test_*.py"`
Expected: `OK` — 411 + the new tests.

- [ ] **Step 6: Commit**

```bash
git add cas/theme.py tests/test_ui.py
git commit -m "feat(gui): flat ttk theme (cas/theme.py) — palette, type scale, taller rows, no deps

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `gui` pure helpers — selection rule, menu gating, cells

**Files:**
- Modify: `cas/gui.py` (add module-level functions near `_manifest_from_axes`, around line 140)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: nothing.
- Produces (all pure, all used by Tasks 5 and 6):
  - `gui._selection_summary(n_selected, n_total) -> str`
  - `gui._rightclick_selection(clicked, current) -> tuple[str, ...]`
  - `gui._context_actions(n_selected, state) -> dict[str, bool]` — keys: `save`, `assign_profile`, `assign_firmware`, `run_root`, `run_download`, `run_warmup`, `run_lock`, `seal`, `release`, `copy_serial`
  - `gui._profile_cell(name, manual) -> str`
  - `gui._state_cell(state) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ui.py`:

```python
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
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd tests && python -m unittest test_ui -v`
Expected: FAIL — `ImportError: cannot import name '_selection_summary' from 'cas.gui'`

- [ ] **Step 3: Add the helpers to `cas/gui.py`**

Insert directly below `_manifest_from_axes` (currently `cas/gui.py:140-144`):

```python
def _selection_summary(n_sel, n_total):
    """The footer's blast-radius line. ▶ Run always targets the SELECTION, so the operator must be
    able to read what that is without counting rows. Pure (no Tk)."""
    if n_total == 0:
        return "No devices connected"
    if n_sel == 0:
        return f"No devices selected  ·  {n_total} connected"
    if n_sel == n_total:
        return f"ALL {n_total} devices selected"
    return f"{n_sel} of {n_total} devices selected"


def _rightclick_selection(clicked, current):
    """File-manager rule for a right-click: a click OUTSIDE the selection replaces it with the clicked
    row; a click INSIDE a multi-selection keeps the whole selection (so 'Run ▸ Download' hits all of
    them); a click on empty space leaves the selection alone. Tk's Treeview does none of this by
    itself — without it, right-clicking one of five selected rows would silently drop the other four.
    Returns the selection to apply. Pure (no Tk)."""
    cur = tuple(current)
    if not clicked:
        return cur
    if clicked in cur:
        return cur
    return (clicked,)


def _context_actions(n_selected, state):
    """Which context-menu items are enabled for the current selection. `state` is the adb state of the
    focused row (only the single-device items care). Pure (no Tk) so the gating is unit-tested."""
    one = n_selected == 1
    any_ = n_selected >= 1
    online = state == "device"
    return {
        "save":            one and online,      # capture reads the device over adb
        "assign_profile":  any_,
        "assign_firmware": any_,
        "run_root":        any_,                # a fastboot/EDL unit is a legitimate Root target
        "run_download":    any_,
        "run_warmup":      any_,
        "run_lock":        any_,
        "seal":            one,
        "release":         one and online,      # clears the Companion lockdown over adb
        "copy_serial":     any_,
    }


def _profile_cell(name, manual):
    """The 'profile' column text. A MANUAL (sticky, operator-set) assignment is marked in TEXT rather
    than by row colour, because row colour now carries the device STATE — two signals, one row, so
    they can't both be a tint."""
    if not name or name == "(no match)":
        return name or ""
    return f"{name}  (pinned)" if manual else name


def _state_cell(state):
    """The 'state' column text: a dot + the adb state (the dot's COLOUR comes from the row tag)."""
    return f"● {state or '?'}"
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `cd tests && python -m unittest test_ui -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cas/gui.py tests/test_ui.py
git commit -m "feat(gui): pure helpers for selection summary, right-click selection, menu gating

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `cas/dialogs.py` — `ProfilePicker` (Save step 1)

**Files:**
- Create: `cas/dialogs.py`
- Modify: `cas/gui.py` — `run_chain()` (line ~1422), `new_profile()` (line ~2317)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `theme` (not required — the picker inherits the styles), `cas.profiles as P`.
- Produces:
  - `dialogs.profile_rows(profiles_root) -> list[dict]` — each `{"name", "model", "captured", "has_golden"}`, sorted by name (the order `P.list_profiles` returns).
  - `dialogs.overwrite_warning(row) -> str` — `""` when the profile has no golden.
  - `dialogs.ProfilePicker(parent, profiles_root, title=…, preselect=None, warn_overwrite=True, on_new=None, ok_text="Next ›")` with attribute `.result` → the chosen profile name, or `None` if cancelled.
  - `gui.App._ask_save_profile(serial) -> str | None`
  - `gui.App.new_profile() -> str | None` (**changed**: now RETURNS the created name)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ui.py`:

```python
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
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `cd tests && python -m unittest test_ui -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cas.dialogs'`

- [ ] **Step 3: Write `cas/dialogs.py`**

```python
"""Every CAS window that is NOT the main window: the profile picker, the Profiles and Firmware
library windows, and the ES-DE box-art setting.

These absorbed the old right-hand panel. The main window now shows ONE thing — the device list —
and these windows manage the libraries behind it. The row-builders are pure (filesystem only, no Tk)
so they unit-test without a display; sizing a multi-GB golden is done on a worker thread, never on
the UI thread (the library may live on a slow external drive).
"""
import pathlib
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from . import profiles as P


def profile_rows(profiles_root):
    """One row per profile in the library: name, model_match, capture date, whether a golden exists.
    Deliberately does NOT size the golden — that's a full directory walk (multi-GB, possibly over a
    slow external drive) and would freeze the picker. The dialog fills sizes on a worker thread."""
    rows = []
    for prof in P.list_profiles(profiles_root):
        rows.append({
            "name": prof.name,
            "model": prof.meta.get("model_match", "") or "",
            "captured": prof.meta.get("captured", "") or "",
            "has_golden": prof.has_golden(),
        })
    return rows


def overwrite_warning(row):
    """The inline warning shown when the highlighted profile already holds a golden. A WARNING, not a
    block — re-capturing a master into its own profile is the normal flow."""
    if not row or not row.get("has_golden"):
        return ""
    return (f"⚠  “{row['name']}” already has a golden — saving will REPLACE it.")


def _human_size(nbytes):
    """Bytes -> '3.4 GB' / '512 MB' / '—' (mirrors gui._human_size; kept here so dialogs.py has no
    import back into gui.py — that would be a cycle)."""
    n = float(nbytes or 0)
    if n <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _center(dlg, parent, dy=90):
    dlg.update_idletasks()
    try:
        x = parent.winfo_rootx() + (parent.winfo_width() - dlg.winfo_width()) // 2
        y = parent.winfo_rooty() + dy
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
    except tk.TclError:
        pass


class ProfilePicker:
    """Modal: choose the profile to Save into (or to assign to a device).

    Replaces the old free-text 'type the profile name' prompt — the operator picks from what the
    library actually holds, sees which profiles already carry a golden, and is warned inline before
    overwriting one. `.result` is the chosen name, or None if cancelled.

    on_new: a callable that creates a profile and returns its name (gui.App.new_profile), or None to
    hide the '＋ New profile…' button. warn_overwrite: show the golden-replace warning (Save wants
    it; assigning a profile to a device does not)."""

    def __init__(self, parent, profiles_root, title="Save — which profile?", preselect=None,
                 warn_overwrite=True, on_new=None, ok_text="Next ›"):
        self.result = None
        self.profiles_root = profiles_root
        self.warn_overwrite = warn_overwrite
        self.on_new = on_new
        self._rows = {}                                   # name -> row dict

        self.win = win = tk.Toplevel(parent)
        win.title(title)
        win.transient(parent)
        win.minsize(520, 340)

        ttk.Label(win, text=title, style="Title.TLabel").pack(anchor="w", padx=12, pady=(12, 2))
        ttk.Label(win, text="The device's assigned profile is pre-selected. Double-click to confirm.",
                  style="Muted.TLabel").pack(anchor="w", padx=12, pady=(0, 8))

        self.tree = ttk.Treeview(win, columns=("model", "golden", "captured"),
                                 show="tree headings", selectmode="browse", height=9)
        self.tree.heading("#0", text="profile")
        self.tree.column("#0", width=190)
        for c, t, w in (("model", "model", 130), ("golden", "golden", 110), ("captured", "captured", 100)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, padx=12)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.bind("<Double-1>", lambda e: self._ok())

        self.warn_var = tk.StringVar(value="")
        ttk.Label(win, textvariable=self.warn_var, style="Warn.TLabel",
                  wraplength=480, justify="left").pack(anchor="w", padx=12, pady=(8, 0))

        bar = ttk.Frame(win, padding=(12, 10))
        bar.pack(fill="x", side="bottom")
        if on_new:
            ttk.Button(bar, text="＋ New profile…", command=self._new).pack(side="left")
        self.ok_btn = ttk.Button(bar, text=ok_text, style="Accent.TButton", command=self._ok)
        self.ok_btn.pack(side="right")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 8))

        self._reload(preselect)
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Return>", lambda e: self._ok())
        _center(win, parent)
        win.grab_set()
        win.wait_window()

    def _reload(self, preselect=None):
        self.tree.delete(*self.tree.get_children())
        self._rows = {}
        rows = profile_rows(self.profiles_root)
        for r in rows:
            self._rows[r["name"]] = r
            self.tree.insert("", "end", iid=r["name"], text=r["name"],
                             values=(r["model"] or "—",
                                     "sizing…" if r["has_golden"] else "— empty",
                                     r["captured"] or "—"))
        if not rows:
            self.warn_var.set("No profiles in the library yet — use “＋ New profile…”.")
        target = preselect if preselect in self._rows else (rows[0]["name"] if rows else None)
        if target:
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)
        self._size_goldens([r["name"] for r in rows if r["has_golden"]])
        self._on_select()

    def _size_goldens(self, names):
        """Fill the 'golden' column off-thread — a golden is multi-GB and the library may be on a slow
        external drive; walking it on the UI thread would freeze the picker."""
        def work():
            for name in names:
                try:
                    b = P.Profile(pathlib.Path(self.profiles_root) / name).golden_size()
                except OSError:
                    continue
                self.win.after(0, lambda n=name, size=b: self._set_size(n, size))
        threading.Thread(target=work, daemon=True).start()

    def _set_size(self, name, nbytes):
        try:
            if self.tree.exists(name):
                vals = list(self.tree.item(name).get("values") or ["", "", ""])
                vals[1] = _human_size(nbytes)
                self.tree.item(name, values=vals)
        except tk.TclError:
            pass                                          # picker closed mid-size — nothing to update

    def _selected(self):
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def _on_select(self):
        row = self._selected()
        self.ok_btn.configure(state="normal" if row else "disabled")
        self.warn_var.set(overwrite_warning(row) if self.warn_overwrite else "")

    def _new(self):
        name = self.on_new()
        if name:
            self._reload(preselect=name)

    def _ok(self):
        row = self._selected()
        if not row:
            return
        self.result = row["name"]
        self.win.destroy()
```

- [ ] **Step 4: Wire Save to the picker in `cas/gui.py`**

Add the import beside the other `from . import …` lines at the top of `cas/gui.py`:

```python
from . import dialogs as D
```

Replace the `save` branch of `run_chain()` (`cas/gui.py:1427-1441`) with a call to a new `_run_save`, so the footer's `① Save` and (Task 5) the context menu's Save go through ONE path:

```python
    def run_chain(self):
        steps, err = self._resolve_chain({k: v.get() for k, v in self.chain_vars.items()})
        if err:
            messagebox.showinfo("CAS", err)
            return
        if "save" in steps:
            serial = self._selected_serial()
            if not serial:
                messagebox.showinfo("CAS", "Select ONE golden device for Save.")
                return
            self._run_save(steps, serial)
        else:
            t = self._action_targets()
            if not t:
                return
            cleared = self._preflight(steps, t)           # skip hard-blocked, confirm risky, then run
            if not cleared:
                return
            if "download" in steps and not self._pick_downloads(cleared):
                return
            self._run_chain(steps, cleared)

    def _run_save(self, steps, serial):
        """Save = preflight → pick the DESTINATION PROFILE (modal) → pick what to capture (modal) → run.
        Both entry points (the ① Save tick and the context menu) land here, so they can't drift."""
        cleared = self._preflight(steps, [serial])        # gate before either modal
        if not cleared:
            return
        name = self._ask_save_profile(serial)
        if not name:
            return
        if not self._pick_capture(serial, name):          # modal: choose what to capture (or cancel)
            return
        self._run_chain(steps, cleared, save_name=name)

    def _ask_save_profile(self, serial):
        """Step 1 of Save: pick the destination profile, pre-selecting the device's assigned one.
        Replaces the old 'type the profile name' text prompt."""
        cur = self.assigned.get(serial)
        pick = D.ProfilePicker(
            self.win, self.profiles_root,
            title=f"Save — capture {self._row_model(serial)} into which profile?",
            preselect=(cur if cur and cur != "(no match)" else None),
            warn_overwrite=True, on_new=self.new_profile)
        return pick.result
```

Make `new_profile()` RETURN the name it created (it currently returns `None`), so the picker can select it. In `cas/gui.py:2317`, change the three early `return` statements to `return None` and add a final `return name`:

```python
    def new_profile(self):
        """Create an empty profile and RETURN its name (the ProfilePicker selects the result), or None
        if the operator cancelled / the name was taken."""
        name = simpledialog.askstring(
            "New profile",
            "New profile name — include the device model and SD size so it auto-matches, e.g.:\n"
            "   retroid-pocket-6-512   ·   retroid-pocket-6-256   ·   odin2-mini")
        if not name:
            return None
        d = P.pathlib.Path(self.profiles_root) / name
        if d.exists():
            messagebox.showerror("CAS", "A profile with that name already exists.")
            return None
        d.mkdir(parents=True)
        (d / "profile.meta").write_text("model_match=\nfrontend=\nnotes=\ncaptured=\n")
        (d / "manifest").write_text(f"# {name} (empty — capture a golden to populate)\n")
        self.log(f"created profile '{name}' — auto-matches by name + SD size, no regex needed.")
        self.refresh_profiles()
        return name
```

Note: the old body also did `self.prof_var.set(name)` and `self.on_select_profile()`. Both refer to the right-panel combobox and are deleted here — Task 6 removes `prof_var` entirely. Leave `refresh_profiles()` in place; Task 6 redefines it.

- [ ] **Step 5: Run the tests**

Run: `cd tests && python -m unittest discover -p "test_*.py"`
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add cas/dialogs.py cas/gui.py tests/test_ui.py
git commit -m "feat(gui): Save picks its destination profile in a modal, not a text prompt

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Profiles / Firmware / box-art windows

**Files:**
- Modify: `cas/dialogs.py` (append), `cas/gui.py` (add the three opener methods)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `dialogs.profile_rows`, `dialogs._human_size`, `dialogs._center` (Task 3).
- Produces:
  - `dialogs.firmware_rows(fw_root) -> list[dict]` — each `{"id", "version", "match", "device", "target"}`.
  - `dialogs.ProfilesWindow(parent, app)` — `app` supplies `.profiles_root`, `.new_profile()`, `.delete_profile()`, `.log()`, `._open_path()`, `.refresh_devices()`.
  - `dialogs.FirmwareWindow(parent, app)` — `app` supplies `.log()`, `._add_firmware()`, `._open_path()`.
  - `dialogs.BoxArtDialog(parent, app)` — `app` supplies `.log()`, `._probe_sd_media()`, `.sd_media_var`.
  - `gui.App._open_profiles()`, `gui.App._open_firmware()`, `gui.App._open_boxart()`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ui.py`:

```python
class TestFirmwareRows(unittest.TestCase):
    def _library(self, td):
        """A firmware library shaped the way cas.firmware.ingest() writes it."""
        import json
        root = pathlib.Path(td) / "_firmware"
        fw = root / "mangmi-air-x-mq66"
        (fw / "v1").mkdir(parents=True)
        (fw / "firmware.json").write_text(json.dumps({
            "id": "mangmi-air-x-mq66", "device": "AIR X", "storage": "emmc",
            "flash_target": "init_boot", "current": "v1", "versions": ["v1"],
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

    def test_an_empty_library_yields_no_rows(self):
        from cas.dialogs import firmware_rows
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(firmware_rows(pathlib.Path(td)), [])
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd tests && python -m unittest test_ui -v`
Expected: FAIL — `ImportError: cannot import name 'firmware_rows'`

> If the assertions about `firmware.json`'s shape fail instead, read `cas/firmware.py:127-160`
> (`Firmware.current()` / `.versions()` / `.match_rules()`) and fix the *test fixture* to match the
> real on-disk shape — never the production code.

- [ ] **Step 3: Append the three windows to `cas/dialogs.py`**

Add `from . import firmware as FW` and `from . import config` to the imports, then:

```python
def firmware_rows(fw_root):
    """One row per firmware id in the library: id, current version, match rules, device, flash target.
    Pure (filesystem only). An unreachable/absent root yields [] — the window says so rather than
    raising."""
    rows = []
    try:
        fws = FW.list_firmware(fw_root)
    except Exception:
        return []
    for fw in fws:
        rules = fw.match_rules() or {}
        pref = ", ".join(rules.get("serial_prefix", []) or []) or "—"
        rows.append({
            "id": fw.id,
            "version": fw.current() or "—",
            "match": pref,
            "device": fw.device() or "—",
            "target": fw.flash_target() or "—",
        })
    return rows


class ProfilesWindow:
    """The profile library — what the right panel's Profile box used to hold, minus the per-device
    assignment (that's on the device row's context menu now). Lists every profile with its golden and
    capture date; New / Delete / Open folder."""

    def __init__(self, parent, app):
        self.app = app
        self.win = win = tk.Toplevel(parent)
        win.title("CAS — profile library")
        win.transient(parent)
        win.geometry("720x420")

        ttk.Label(win, text="Profile library", style="Title.TLabel").pack(anchor="w", padx=12, pady=(12, 0))
        self.lib_var = tk.StringVar(value=f"Library: {app.profiles_root}")
        ttk.Label(win, textvariable=self.lib_var, style="Muted.TLabel").pack(anchor="w", padx=12)

        self.tree = ttk.Treeview(win, columns=("model", "golden", "captured"),
                                 show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="profile"); self.tree.column("#0", width=200)
        for c, t, w in (("model", "model", 150), ("golden", "golden", 130), ("captured", "captured", 110)):
            self.tree.heading(c, text=t); self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, padx=12, pady=(8, 0))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        self.detail_var = tk.StringVar(value="Select a profile to see its golden.")
        ttk.Label(win, textvariable=self.detail_var, style="Muted.TLabel",
                  wraplength=680, justify="left").pack(anchor="w", padx=12, pady=(6, 0))

        bar = ttk.Frame(win, padding=(12, 10))
        bar.pack(fill="x", side="bottom")
        ttk.Button(bar, text="New…", command=self._new).pack(side="left")
        ttk.Button(bar, text="Delete…", command=self._delete).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Open folder", command=lambda: app._open_path(app.profiles_root)) \
            .pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        win.bind("<Escape>", lambda e: win.destroy())
        self.refresh()

    def refresh(self, preselect=None):
        self.tree.delete(*self.tree.get_children())
        rows = profile_rows(self.app.profiles_root)
        for r in rows:
            self.tree.insert("", "end", iid=r["name"], text=r["name"],
                             values=(r["model"] or "—",
                                     "saved" if r["has_golden"] else "— none",
                                     r["captured"] or "—"))
        if preselect and self.tree.exists(preselect):
            self.tree.selection_set(preselect)

    def _selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _on_select(self):
        """Golden size + download ETA for the highlighted profile — sized off-thread (multi-GB walk)."""
        name = self._selected()
        if not name:
            self.detail_var.set("Select a profile to see its golden.")
            return
        prof = P.Profile(pathlib.Path(self.app.profiles_root) / name)
        if not prof.has_golden():
            self.detail_var.set(f"{name}: no golden yet — capture one with ① Save.")
            return
        self.detail_var.set(f"{name}: golden saved · sizing…")

        def work():
            b = prof.golden_size()
            mbps = config.download_mbps(prof.name)
            eta = (f" · ~{int((b / 1048576.0) / mbps // 60)}m to download (avg {mbps:.0f} MB/s)"
                   if (mbps and b) else " · download time estimated after the first Download")
            self.win.after(0, lambda: self.detail_var.set(
                f"{name}: golden saved · {_human_size(b)}{eta}"))
        threading.Thread(target=work, daemon=True).start()

    def _new(self):
        name = self.app.new_profile()
        if name:
            self.refresh(preselect=name)

    def _delete(self):
        name = self._selected()
        if not name:
            messagebox.showinfo("CAS", "Select a profile row to delete.")
            return
        self.app.delete_profile(name)
        self.refresh()


class FirmwareWindow:
    """The device-root-firmware library — the old 'Root images' tab, minus the per-device assignment
    (that's on the device row's context menu now). CAS stores and suggests firmware here; assigning it
    to a unit is what makes Root/Lock use it."""

    def __init__(self, parent, app):
        self.app = app
        self.win = win = tk.Toplevel(parent)
        win.title("CAS — firmware library")
        win.transient(parent)
        win.geometry("760x400")

        ttk.Label(win, text="Firmware library (device root firmware)",
                  style="Title.TLabel").pack(anchor="w", padx=12, pady=(12, 0))
        self.lib_var = tk.StringVar()
        ttk.Label(win, textvariable=self.lib_var, style="Muted.TLabel",
                  wraplength=720, justify="left").pack(anchor="w", padx=12)

        self.tree = ttk.Treeview(win, columns=("version", "device", "target", "match"),
                                 show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="firmware id"); self.tree.column("#0", width=210)
        for c, t, w in (("version", "current", 90), ("device", "device", 130),
                        ("target", "flashes", 100), ("match", "serial prefix", 140)):
            self.tree.heading(c, text=t); self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, padx=12, pady=(8, 0))

        ttk.Label(win, text="Assign firmware to a unit by right-clicking its row in the device list.",
                  style="Muted.TLabel").pack(anchor="w", padx=12, pady=(6, 0))

        bar = ttk.Frame(win, padding=(12, 10))
        bar.pack(fill="x", side="bottom")
        ttk.Button(bar, text="Add / update…", command=self._add).pack(side="left")
        ttk.Button(bar, text="Open folder", command=self._open).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        win.bind("<Escape>", lambda e: win.destroy())
        self.refresh()

    def _root(self):
        try:
            return FW.firmware_root()
        except Exception:
            return None

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        root = self._root()
        rows = firmware_rows(root) if root else []
        for r in rows:
            self.tree.insert("", "end", iid=r["id"], text=r["id"],
                             values=(r["version"], r["device"], r["target"], r["match"]))
        # Explain an EMPTY list instead of leaving it a mystery: the configured (shared/external)
        # firmware dir may simply be unmounted, in which case firmware_root() silently falls back.
        configured = config.load_config().get("firmware_dir")
        def _isdir(p):
            try:
                return bool(p) and pathlib.Path(p).is_dir()
            except OSError:
                return False
        if configured and not _isdir(configured):
            self.lib_var.set(f"Library: {configured}   ✗ unreachable (library drive unmounted?) — "
                             f"falling back to {root}")
        elif not _isdir(root):
            self.lib_var.set(f"Library: {root}   ✗ not reachable (connect the library drive?)")
        elif not rows:
            self.lib_var.set(f"Library: {root}   ✓ (no firmware yet — use “Add / update…”)")
        else:
            self.lib_var.set(f"Library: {root}   ✓ ({len(rows)} firmware)")

    def _add(self):
        self.app._add_firmware()                      # threaded ingest; refresh when it lands
        self.win.after(1500, self.refresh)

    def _open(self):
        root = self._root()
        if root:
            self.app._open_path(str(root))


class BoxArtDialog:
    """The ES-DE box-art source — a BENCH setting (config.es_media_src), not a per-profile one, which
    is why it left the profile panel. Either each unit's own SD carries the art, or CAS pushes it from
    a PC folder on Download."""

    def __init__(self, parent, app):
        self.app = app
        self.win = win = tk.Toplevel(parent)
        win.title("CAS — ES-DE box art")
        win.transient(parent)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="ES-DE box art", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frm, text="Where the box art comes from when a unit gets ES-DE on Download.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 10))

        ttk.Radiobutton(frm, text="Use the SD card  (no transfer — box art rides the SD image)",
                        value="sd", variable=app.media_mode,
                        command=app._on_media_mode).pack(anchor="w")
        prow = ttk.Frame(frm)
        prow.pack(fill="x", pady=(6, 0))
        ttk.Radiobutton(prow, text="Push from PC folder:", value="push",
                        variable=app.media_mode, command=app._on_media_mode).pack(side="left")
        ttk.Entry(prow, textvariable=app.media_path, width=30).pack(side="left", padx=(4, 0))
        ttk.Button(prow, text="Browse…", command=app._browse_media).pack(side="left", padx=4)

        ttk.Label(frm, textvariable=app.sd_media_var, style="Muted.TLabel",
                  wraplength=430, justify="left").pack(anchor="w", pady=(10, 0))
        ttk.Button(frm, text="Close", command=win.destroy).pack(anchor="e", pady=(14, 0))
        win.bind("<Escape>", lambda e: win.destroy())
        _center(win, parent)
        app._probe_sd_media()                          # refresh the SD status while the dialog is open
```

- [ ] **Step 4: Add the three openers to `cas/gui.py`**

Put them next to `_open_apk_store` (around `cas/gui.py:1668`):

```python
    def _open_profiles(self):
        D.ProfilesWindow(self.win, self)

    def _open_firmware(self):
        D.FirmwareWindow(self.win, self)

    def _open_boxart(self):
        D.BoxArtDialog(self.win, self)
```

`ProfilesWindow` calls `app.delete_profile(name)`, but today `delete_profile()` reads the name from
the combobox. Give it a parameter (`cas/gui.py:2342`):

```python
    def delete_profile(self, name=None):
        """Archive a profile (never delete). Deliberately hard: the operator must type the exact name."""
        if not name:
            return
        typed = simpledialog.askstring(
            "Delete profile (archives it)",
            f"This ARCHIVES '{name}' to profiles/_archive (recoverable).\n"
            f"Type the profile name exactly to confirm:")
        if typed != name:
            self.log("delete cancelled (name did not match).")
            return
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
        dst = P.archive_profile(prof, _stamp())
        for s in [s for s, n in list(self.assigned.items()) if n == name]:
            self.assigned.pop(s, None)
            self.assigned_manual.discard(s)
            config.set_device_profile(s, None)
        self.log(f"archived '{name}' -> {dst}")
        self.refresh_profiles()
        self.refresh_devices()
```

- [ ] **Step 5: Run the tests**

Run: `cd tests && python -m unittest discover -p "test_*.py"`
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add cas/dialogs.py cas/gui.py tests/test_ui.py
git commit -m "feat(gui): Profiles, Firmware and box-art windows (absorb the right panel)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Right-click context menu

**Files:**
- Modify: `cas/gui.py` — `assign_profile` (line ~1509), `assign_firmware` (~1626), `unassign_firmware` (~1648), `_assign_on_doubleclick` (~1497)
- Modify: `tests/test_cas.py` — lines ~4538 and ~4563
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `gui._rightclick_selection`, `gui._context_actions` (Task 2); `dialogs.ProfilePicker` (Task 3).
- Produces (signature changes — every caller must pass them explicitly now):
  - `App.assign_profile(name, serials)` (was: read the combobox + the tree selection)
  - `App.unassign_profile(serials)` (**new** — clears the manual override, back to auto-match)
  - `App.assign_firmware(fid, serials)` (was: read `fw_var` + the tree selection)
  - `App.unassign_firmware(serials)` (was: read the tree selection)
  - `App._build_context_menu()`, `App._popup_context(event, keyboard=False)`, `App._rebuild_context_menu(serials)`, `App._ctx_run(step, serials)`, `App._copy_serials(serials)`, `App.select_all_devices()`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ui.py`:

```python
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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd tests && python -m unittest test_ui -v`
Expected: FAIL — `AssertionError: ['serials'] != ['name', 'serials']` and `AttributeError: 'App' object has no attribute 'unassign_profile'`

- [ ] **Step 3: Re-sign the assignment methods in `cas/gui.py`**

Replace `assign_profile` (whole method) and `_assign_on_doubleclick`:

```python
    def _assign_on_doubleclick(self, event):
        """Double-click a device row → pick a profile for THAT row (the fast manual-override path)."""
        row = self.dev_tree.identify_row(event.y)
        if not row:
            return
        self.dev_tree.selection_set(row)
        self._ctx_pick_profile([row])

    def _ctx_pick_profile(self, serials):
        """Open the profile picker and assign the result to `serials` (no overwrite warning — nothing
        is being captured here)."""
        cur = self.assigned.get(serials[0]) if len(serials) == 1 else None
        pick = D.ProfilePicker(
            self.win, self.profiles_root,
            title=f"Assign a profile to {len(serials)} device(s)",
            preselect=(cur if cur and cur != "(no match)" else None),
            warn_overwrite=False, on_new=self.new_profile, ok_text="Assign")
        if pick.result:
            self.assign_profile(pick.result, serials)

    def assign_profile(self, name, serials):
        """Assign profile `name` to `serials` as a sticky MANUAL override (always wins over the model
        auto-match, remembered across launches). Warns first when the profile's model_match doesn't fit
        a selected device — Root/Lock FLASH that profile's init_boot, so a wrong pairing can bootloop."""
        if not serials or not name:
            return
        mm = P.Profile(P.pathlib.Path(self.profiles_root) / name).meta.get("model_match")
        mismatched = []
        for s in serials:
            vals = self.dev_tree.item(s).get("values") or []
            model = str(vals[0]) if vals else ""
            if model and mm and not re.search(mm, model):
                mismatched.append(f"{s} ({model})")
        warn = ""
        if mismatched:
            warn = ("\n\n⚠ This profile targets '" + mm + "', but these don't match:\n  "
                    + "\n  ".join(mismatched)
                    + "\nRoot/Lock will FLASH this profile's init_boot on them — fine if same chipset, "
                      "could bootloop if not. Assign anyway?")
        if not messagebox.askyesno(
                "CAS — assign profile",
                f"Set profile '{name}' on {len(serials)} device(s)?\n  " + "\n  ".join(serials) + warn):
            return
        for s in serials:
            self.assigned[s] = name
            self.assigned_manual.add(s)
            config.set_device_profile(s, name, manual=True)      # sticky across launches
            if self.dev_tree.exists(s):
                vals = list(self.dev_tree.item(s).get("values") or ["", "", "", "", ""])
                vals[2] = _profile_cell(name, True)
                self.dev_tree.item(s, values=vals)
        self.log(f"assigned profile '{name}' to: {', '.join(serials)} (pinned, remembered)")

    def unassign_profile(self, serials):
        """Clear the MANUAL profile override on `serials` — they go back to auto-matching by model +
        SD tier on every refresh."""
        if not serials:
            return
        if not messagebox.askyesno(
                "CAS — clear profile override",
                f"Clear the pinned profile on {len(serials)} device(s)?\n  " + "\n  ".join(serials)
                + "\n\nThey go back to auto-matching by model + SD size."):
            return
        for s in serials:
            self.assigned_manual.discard(s)
            config.set_device_profile(s, None)
        self.log(f"cleared the profile override on: {', '.join(serials)} (re-matching…)")
        self.refresh_devices()
```

Re-sign the firmware pair (they currently read `fw_var` and `self.dev_tree.selection()`):

```python
    def assign_firmware(self, fid, serials):
        """Assign firmware `fid` to `serials` as a sticky MANUAL override (always wins over the
        serial-prefix auto-match)."""
        if not serials or not fid:
            return
        if not messagebox.askyesno(
                "CAS — assign firmware",
                f"Set firmware '{fid}' on {len(serials)} device(s) as a manual override?\n  "
                + "\n  ".join(serials)):
            return
        for s in serials:
            FW.set_device_firmware(s, fid, manual=True)
            FW.log_event(s, fid, None, "assign", True)
        self.log(f"assigned firmware '{fid}' to: {', '.join(serials)} (remembered). Re-resolving…")
        self.refresh_devices()

    def unassign_firmware(self, serials):
        """Clear the firmware override on `serials`. Root then auto-matches, or uses the bundled
        DEFAULT init_boot kit when nothing matches — the right move when the only library match is a
        wrong-platform image."""
        if not serials:
            return
        if not messagebox.askyesno(
                "CAS — clear firmware override",
                f"Clear the firmware override on {len(serials)} device(s)?\n  " + "\n  ".join(serials)
                + "\n\nRoot then uses the auto-match, or the DEFAULT init_boot kit if nothing matches."):
            return
        for s in serials:
            FW.set_device_firmware(s, None)
            FW.log_event(s, None, None, "clear", False)
        self.log(f"cleared firmware override on: {', '.join(serials)} (re-resolving…)")
        self.refresh_devices()
```

- [ ] **Step 4: Build the context menu (new methods in `cas/gui.py`)**

Add after `_assign_on_doubleclick`:

```python
    # ---------- right-click context menu (per-device actions on the SELECTION) ----------
    def _build_context_menu(self):
        """Two menus: one for a row (rebuilt per right-click, since the items depend on the selection)
        and a small one for empty space."""
        self.ctx = tk.Menu(self.win, tearoff=0)
        self._ctx_subs = []                      # live submenu refs — Tk GCs an unreferenced Menu
        self.ctx_empty = tk.Menu(self.win, tearoff=0)
        self.ctx_empty.add_command(label="⟳ Refresh devices", command=self.refresh_devices)
        self.ctx_empty.add_command(label="Select all", command=self.select_all_devices)
        self.dev_tree.bind("<Button-3>", self._popup_context)
        self.dev_tree.bind("<Shift-F10>", lambda e: self._popup_context(e, keyboard=True))
        if sys.platform == "darwin":
            self.dev_tree.bind("<Button-2>", self._popup_context)     # the mac right button

    def _popup_context(self, event, keyboard=False):
        row = self.dev_tree.focus() if keyboard else self.dev_tree.identify_row(event.y)
        sel = _rightclick_selection(row, self.dev_tree.selection())
        if not sel:
            self._post_menu(self.ctx_empty, event)
            return "break"
        self.dev_tree.selection_set(sel)
        self.dev_tree.focus(sel[0])
        self._rebuild_context_menu(list(sel))
        self._post_menu(self.ctx, event)
        return "break"

    def _post_menu(self, menu, event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _row_state(self, serial):
        vals = self.dev_tree.item(serial).get("values") or []
        return str(vals[4]).replace("● ", "") if len(vals) > 4 else ""

    def _rebuild_context_menu(self, serials):
        m = self.ctx
        m.delete(0, "end")
        self._ctx_subs = []
        one = serials[0] if len(serials) == 1 else None
        en = _context_actions(len(serials), self._row_state(one) if one else "device")
        st = lambda k: ("normal" if en[k] else "disabled")           # noqa: E731 — a local alias

        m.add_command(label=(f"Save “{one}” → profile…" if one else "Save → profile…  (select ONE device)"),
                      state=st("save"), command=lambda s=one: self._run_save(["save"], s))
        m.add_separator()
        m.add_cascade(label="Assign profile", menu=self._profile_submenu(serials))
        m.add_cascade(label="Assign firmware", menu=self._firmware_submenu(serials))
        m.add_separator()
        run = tk.Menu(m, tearoff=0)
        for key, label in (("root", "⓪ Root"), ("download", "② Download"),
                           ("warmup", "③ Warm up"), ("lock", "④ Lock")):
            run.add_command(label=label, state=st("run_" + key),
                            command=lambda k=key, ss=list(serials): self._ctx_run(k, ss))
        self._ctx_subs.append(run)
        m.add_cascade(label=f"Run on {len(serials)} device(s)", menu=run)
        m.add_separator()
        m.add_command(label="Seal (retail lock)…", state=st("seal"), command=self.seal_selected)
        m.add_command(label="Release (un-provision)…", state=st("release"), command=self.release_selected)
        m.add_separator()
        m.add_command(label=("Copy serial" if one else f"Copy {len(serials)} serials"),
                      state=st("copy_serial"), command=lambda ss=list(serials): self._copy_serials(ss))

    def _profile_submenu(self, serials):
        sub = tk.Menu(self.ctx, tearoff=0)
        current = {self.assigned.get(s) for s in serials}
        shown = current.pop() if len(current) == 1 else None      # a common assignment gets the ●
        names = [p.name for p in P.list_profiles(self.profiles_root)]
        for name in names:
            sub.add_command(label=("●  " if name == shown else "○  ") + name,
                            command=lambda n=name, ss=list(serials): self.assign_profile(n, ss))
        if not names:
            sub.add_command(label="(no profiles — open Profiles…)", state="disabled")
        sub.add_separator()
        sub.add_command(label="Auto-match (clear the pin)",
                        command=lambda ss=list(serials): self.unassign_profile(ss))
        sub.add_command(label="＋ New profile…", command=lambda ss=list(serials): self._ctx_pick_profile(ss))
        self._ctx_subs.append(sub)
        return sub

    def _firmware_submenu(self, serials):
        sub = tk.Menu(self.ctx, tearoff=0)
        current = {(self.fw_resolved.get(s) or {}).get("firmware_id") for s in serials}
        shown = current.pop() if len(current) == 1 else None
        try:
            ids = [f.id for f in FW.list_firmware(FW.firmware_root())]
        except Exception:
            ids = []
        for fid in [FW.DEFAULT_FW_ID] + ids:                 # the bundled kit is a first-class choice
            sub.add_command(label=("●  " if fid == shown else "○  ") + fid,
                            command=lambda f=fid, ss=list(serials): self.assign_firmware(f, ss))
        sub.add_separator()
        sub.add_command(label="Clear override (auto-match)",
                        command=lambda ss=list(serials): self.unassign_firmware(ss))
        self._ctx_subs.append(sub)
        return sub

    def _ctx_run(self, step, serials):
        """Run ONE chain step on the selection — the same preflight, report and retry as ▶ Run (it goes
        through _run_chain), just without ticking a box first."""
        cleared = self._preflight([step], serials)
        if not cleared:
            return
        if step == "download" and not self._pick_downloads(cleared):
            return
        self._run_chain([step], cleared)

    def _copy_serials(self, serials):
        self.win.clipboard_clear()
        self.win.clipboard_append("\n".join(serials))
        self.log(f"copied {len(serials)} serial(s) to the clipboard.")

    def select_all_devices(self):
        self.dev_tree.selection_set(self.dev_tree.get_children())
```

- [ ] **Step 5: Update the two existing tests in `tests/test_cas.py`**

At `tests/test_cas.py:4549-4554`, the test stubs `fw_var` and calls `assign_firmware()` with no
argument. Pass the id and the serials instead, and drop the now-unused stubs:

```python
                app.dev_tree = types.SimpleNamespace(selection=lambda: ["2ee078bd"])
                with mock.patch("cas.gui.messagebox.askyesno", return_value=True):
                    app.assign_firmware("ayn-m2", ["2ee078bd"])
```

And at `tests/test_cas.py:4575-4579`:

```python
                app.dev_tree = types.SimpleNamespace(selection=lambda: ["2ee078bd"])
                with mock.patch("cas.gui.messagebox.askyesno", return_value=True):
                    app.unassign_firmware(["2ee078bd"])
```

(The `app.fw_var = types.SimpleNamespace(...)` lines go away — `fw_var` no longer exists.)

- [ ] **Step 6: Run the whole suite**

Run: `cd tests && python -m unittest discover -p "test_*.py"`
Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add cas/gui.py tests/test_ui.py tests/test_cas.py
git commit -m "feat(gui): right-click context menu — assign profile/firmware, run a step, seal, release

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Layout surgery — full-width list, toolbar, selection footer

**Files:**
- Modify: `cas/gui.py` — `_build()` (lines 501-702), `_build_menu()` (204-238), `_populate_devices` (963-1002), `refresh_profiles` (877-896), `_action_targets` (1460-1469), `_run_bg`'s `done()` (775-804)
- Delete from `cas/gui.py`: `_on_batch_toggle`, `_sync_media_tab`, `_update_fw_status`, `_update_golden_status`, `on_select_profile`, `refresh_firmware`, and the `batch_var` / `prof_var` / `prof_combo` / `fw_var` / `fw_combo` / `nb` / `media_tab` / `golden_var` / `fw_lib_var` / `fw_status_var` attributes.
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `_selection_summary`, `_profile_cell`, `_state_cell` (Task 2); `_build_context_menu` (Task 5); `_open_profiles` / `_open_firmware` / `_open_boxart` (Task 4).
- Produces: `App._on_tree_select()`, `App._update_run_state()`, `App.clear_selection()`; `App.sel_var` (StringVar), `App.lib_var` (StringVar, now in the toolbar).

**Keep** `media_mode`, `media_path`, `sd_media_var`, `_on_media_mode`, `_browse_media`, `_probe_sd_media` — `BoxArtDialog` uses all six; they just aren't packed in the main window any more. Create the three vars in `_build()` without packing widgets for them.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ui.py`:

```python
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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd tests && python -m unittest test_ui -v`
Expected: FAIL — `_action_targets` reads `self.batch_var`, so `AttributeError: 'App' object has no attribute 'batch_var'`; and `test_the_batch_toggle_is_gone` fails.

- [ ] **Step 3: Replace `_build()` in `cas/gui.py`**

Delete lines 501-702 (the whole `_build`) and put this in their place:

```python
    # ---------- layout ----------
    def _build(self):
        """Toolbar (top) · device list (fills) · footer + log (pinned to the bottom).

        The old right-hand panel is GONE: per-device assignment is on the row's context menu, and the
        libraries live in their own windows (Profiles… / Firmware…). One list, full width, because the
        device list is the only thing an operator actually watches."""
        # ── toolbar ───────────────────────────────────────────────────────────────────────────────
        bar = ttk.Frame(self.win, style="Toolbar.TFrame", padding=(10, 8))
        bar.pack(side="top", fill="x")
        _tip(ttk.Button(bar, text="⟳ Refresh", style="Toolbar.TButton", command=self.refresh_devices),
             "Re-scan for plugged-in devices and re-match each one to its profile.").pack(side="left")
        _tip(ttk.Button(bar, text="Profiles…", style="Toolbar.TButton", command=self._open_profiles),
             "The profile library: every saved setup, its golden and when it was captured. Assign one "
             "to a unit by RIGHT-CLICKING the unit's row.").pack(side="left", padx=(8, 0))
        _tip(ttk.Button(bar, text="Firmware…", style="Toolbar.TButton", command=self._open_firmware),
             "The device-root-firmware library. Assign one to a unit by RIGHT-CLICKING the unit's row.") \
            .pack(side="left", padx=(8, 0))
        _tip(ttk.Button(bar, text="Managed APKs…", style="Toolbar.TButton", command=self._open_apk_store),
             "The server APK store: the app builds CAS installs, and which ones are always-installed.") \
            .pack(side="left", padx=(8, 0))
        self.lib_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.lib_var, style="Toolbar.TLabel").pack(side="right")
        self._update_lib_label()

        # ── footer (pinned BOTTOM, packed before the list so a short window never clips ▶ Run) ─────
        footer = ttk.Frame(self.win)
        footer.pack(side="bottom", fill="x")

        selrow = ttk.Frame(footer, padding=(10, 6, 10, 0))
        selrow.pack(side="top", fill="x")
        self.sel_var = tk.StringVar(value=_selection_summary(0, 0))
        ttk.Label(selrow, textvariable=self.sel_var).pack(side="left")
        ttk.Button(selrow, text="Clear", command=self.clear_selection).pack(side="right")
        ttk.Button(selrow, text="Select all", command=self.select_all_devices).pack(side="right", padx=(0, 6))

        act = ttk.Frame(footer, padding=(10, 4))
        act.pack(side="top", fill="x")
        self.chain_vars = {}
        self.chain_cbs = {}
        for key, label, tip in (
            ("root", "⓪ Root", "Root the target(s): flash the assigned firmware's Magisk-patched init_boot + install Magisk."),
            ("save", "① Save", "Capture ONE selected device into a profile (golden). Mutually exclusive with Download/Warm up/Lock."),
            ("download", "② Download", "Install each device's assigned profile (apps + saves/BIOS/settings/grants/homescreen)."),
            ("warmup", "③ Warm up", "Open every installed app once (frontends last) so each emulator initializes against "
                                    "its restored settings and indexes its games."),
            ("lock", "④ Lock", "Retail-seal verified unit(s): hide Dev options, un-root, disable USB debugging."),
        ):
            v = tk.BooleanVar(value=False)
            self.chain_vars[key] = v
            cb = ttk.Checkbutton(act, text=label, variable=v, command=self._on_chain_tick)
            cb.pack(side="left", padx=(0, 10))
            _tip(cb, tip)
            self.chain_cbs[key] = cb
        self.run_btn = ttk.Button(act, text="▶ Run", style="Accent.TButton", command=self.run_chain)
        self.run_btn.pack(side="left", padx=(8, 0))
        _tip(self.run_btn, "Run the ticked actions in order on the SELECTED device(s), in parallel.")
        self.btns = list(self.chain_cbs.values()) + [self.run_btn]
        self.cancel_btn = ttk.Button(act, text="✗ Cancel", command=self._cancel_op, state="disabled")
        self.cancel_btn.pack(side="right")
        _tip(self.cancel_btn,
             "Stop the running operation. Safe during the copy/boot phases; during the brief init_boot "
             "WRITE it asks first, since interrupting a flash can brick the unit.")

        statusf = ttk.Frame(footer, padding=(10, 2, 10, 8))
        statusf.pack(side="top", fill="x")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(statusf, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(statusf, mode="indeterminate", length=210)
        self.progress.pack(side="right")

        # ── log (pinned above the footer) ─────────────────────────────────────────────────────────
        logf = ttk.LabelFrame(self.win, text="Log", padding=6)
        logf.pack(side="bottom", fill="both", expand=False, padx=10, pady=(0, 8))
        self.logbox = scrolledtext.ScrolledText(logf, height=6, state="disabled", wrap="word",
                                                relief="flat", borderwidth=0)
        self.logbox.pack(fill="both", expand=True)

        # ── the selection hint, pinned bottom-wards so it sits directly UNDER the list ────────────
        # (pack order matters: every side="bottom" widget stacks upward from the window bottom, so
        # footer → log → hint puts the hint immediately below the list, which is packed last.)
        hint = ttk.Frame(self.win, padding=(10, 0, 10, 4))
        hint.pack(side="bottom", fill="x")
        ttk.Label(hint, style="Muted.TLabel",
                  text="Right-click a device for its actions  ·  Ctrl-click to add  ·  "
                       "Shift-click for a range  ·  Ctrl+A selects all").pack(side="left")

        # ── device list (LAST → takes every pixel the pinned areas didn't) ────────────────────────
        listf = ttk.Frame(self.win, padding=(10, 8))
        listf.pack(side="top", fill="both", expand=True)
        cols = ("model", "sd", "profile", "firmware", "state")
        self.dev_tree = ttk.Treeview(listf, columns=cols, show="tree headings", selectmode="extended")
        self.dev_tree.heading("#0", text="serial")
        self.dev_tree.column("#0", width=150, minwidth=110)
        for c, t, w in (("model", "model", 150), ("sd", "SD card", 170),
                        ("profile", "profile", 190), ("firmware", "firmware", 190),
                        ("state", "state", 110)):
            self.dev_tree.heading(c, text=t)
            self.dev_tree.column(c, width=w, minwidth=70)
        vsb = ttk.Scrollbar(listf, orient="vertical", command=self.dev_tree.yview)
        self.dev_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.dev_tree.pack(side="left", fill="both", expand=True)
        self.dev_tree.bind("<Double-1>", self._assign_on_doubleclick)
        self.dev_tree.bind("<<TreeviewSelect>>", lambda e: self._on_tree_select())
        self.dev_tree.bind("<Control-a>", lambda e: (self.select_all_devices(), "break")[1])
        self._build_context_menu()

        # Box-art state lives on the App (BoxArtDialog binds to these); no widget in the main window.
        self.media_mode = tk.StringVar(value="push" if config.es_media_src() else "sd")
        self.media_path = tk.StringVar(value=config.es_media_src() or "")
        self.sd_media_var = tk.StringVar(value="")

        self._update_run_state()

    def _on_tree_select(self):
        self.sel_var.set(_selection_summary(len(self.dev_tree.selection()),
                                            len(self.dev_tree.get_children())))
        self._update_run_state()

    def _update_run_state(self):
        """▶ Run is live only when something is selected AND at least one action is ticked — instead of
        popping a 'select a device' box AFTER the click."""
        if self.busy:
            return                                   # _run_bg owns the button while a job is in flight
        ready = bool(self.dev_tree.selection()) and any(v.get() for v in self.chain_vars.values())
        self.run_btn.configure(state="normal" if ready else "disabled")

    def clear_selection(self):
        self.dev_tree.selection_remove(*self.dev_tree.selection())
```

- [ ] **Step 4: Fix the call sites the old panel fed**

`_on_chain_tick` (line ~1409) must also refresh the Run button — append one line to it:

```python
        self.chain_cbs["save"].configure(state="disabled" if unit_on else "normal")
        if unit_on:
            self.chain_vars["save"].set(False)
        self._update_run_state()                     # ← add: ticking an action can arm ▶ Run
```

`_action_targets` (line ~1460) loses the batch branch:

```python
    def _action_targets(self):
        """The serials an action runs on: the SELECTED rows. (There is no apply-to-all mode any more —
        Select all / Ctrl+A does that job, and the footer states the count before you press ▶ Run.)"""
        serials = list(self.dev_tree.selection())
        if not serials:
            messagebox.showinfo("CAS", "Select one or more device rows first "
                                       "(Ctrl-click to add, Shift-click for a range, Ctrl+A for all).")
            return None
        return serials
```

In `_run_bg`'s `done()` (line ~793), re-arm the Run button through the gate instead of forcing it on:

```python
            for b in self.btns:
                b.configure(state="normal")
            self._update_run_state()                 # ← add: an empty selection leaves ▶ Run disabled
```

`refresh_profiles` (line ~877) no longer feeds a combobox:

```python
    def refresh_profiles(self):
        """Re-read the library (the profile windows/pickers list it live, so there's no combobox to
        fill any more) and warn when the library drive isn't there."""
        self._update_lib_label()
        if not self._lib_reachable():
            self.log(f"Library not reachable: {self.profiles_root} — is the library drive connected? "
                     "Use Settings → 'Library folder…' to fix the path.")
```

`refresh_devices`'s `<<TreeviewSelect>>` used to call `_probe_sd_media()` and `_update_fw_status()`;
`_on_tree_select` replaces it (already bound in the new `_build`). **Delete** these methods entirely:
`_on_batch_toggle`, `on_select_profile`, `_update_golden_status`, `_update_fw_status`,
`refresh_firmware`, `_sync_media_tab`, `_scroll_tab`'s callers unchanged (`_app_pick_modal` still uses
it — keep `_scroll_tab`).

Remove the two `refresh_firmware()` calls in `choose_firmware_dir` (lines ~398, ~403) and the one in
`choose_library`'s `_applied()` (line ~414) — that method no longer exists. `_lib_watch` (line ~1997)
also calls it twice; drop both calls there.

`__init__` (line ~197) calls `self.refresh_profiles()` — keep. It does NOT call `refresh_firmware`;
nothing else needs changing there.

Add the box-art entry to the Settings menu in `_build_menu` (after "Managed APKs…", line ~218):

```python
        setm.add_command(label="ES-DE box art…", command=self._open_boxart)
```

- [ ] **Step 5: Teach `_populate_devices` the new cells**

Replace the row-insert block in `_populate_devices` (lines ~974-992) with:

```python
        for i, (serial, model, sd, auto, state) in enumerate(rows):
            if serial in self.assigned_manual:
                shown = self.assigned.get(serial, auto)    # operator override: pinned + remembered
                manual = True
            else:
                shown = auto                               # auto: always reflect the current best match
                manual = False
                prev = self._last_auto.get(serial)
                if prev is not None and prev != auto:
                    changes.append((serial, prev, auto))
                self._last_auto[serial] = auto
                self.assigned[serial] = auto
            tags = ("odd" if i % 2 else "even", "st_" + (state or "other"))
            self.dev_tree.insert("", "end", iid=serial, text=serial,
                                 values=(model, sd, _profile_cell(shown, manual),
                                         self._fw_cell(serial), _state_cell(state)),
                                 tags=tags)
            sn = snaps.get(serial)
            if sn is not None and state == "device":
                gold, mm_ok = self._profile_facts(shown, model)
                sn.update(profile_name=shown, profile_has_golden=gold, profile_model_match_ok=mm_ok)
        # Zebra stripes + a state-coloured row (amber = unauthorized, red = offline). The manual pin is
        # shown in TEXT ('(pinned)'), not colour — one row can't carry two tints.
        self.dev_tree.tag_configure("odd", background=THEME.ZEBRA)
        self.dev_tree.tag_configure("even", background=THEME.LIGHT["surface"])
        for st_name, key in THEME.STATE_COLORS.items():
            self.dev_tree.tag_configure("st_" + st_name, foreground=THEME.LIGHT[key])
        self._on_tree_select()                             # refresh the counter + ▶ Run after a rescan
```

Add the import at the top of `cas/gui.py`:

```python
from . import theme as THEME
```

Finally, delete the `self._probe_sd_media()` call that `_populate_devices` makes just below the block
you replaced (it was there to feed the box-art tab's SD line, which no longer exists in the main
window — `BoxArtDialog` probes when it opens). Leave the `_evaluate_warnings(snaps)` call, the
"refreshed:" log line and the auto-match-changed messagebox exactly as they are.

- [ ] **Step 6: Run the whole suite**

Run: `cd tests && python -m unittest discover -p "test_*.py"`
Expected: `OK`. If a test fails with `AttributeError: prof_var`, a caller of the deleted panel was
missed — grep for it: `grep -n "prof_var\|prof_combo\|fw_var\|fw_combo\|batch_var\|refresh_firmware\|_sync_media_tab\|_update_golden_status\|_update_fw_status\|on_select_profile" cas/gui.py` must come back
empty except the definitions you deliberately kept.

- [ ] **Step 7: Commit**

```bash
git add cas/gui.py tests/test_ui.py
git commit -m "feat(gui): full-width device list, toolbar, selection footer; drop the right panel and the Apply-to-ALL toggle

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Apply the theme, restyle the tooltip, verify on a real window

**Files:**
- Modify: `cas/gui.py` — `main()` (line ~2365), `_Tooltip` (56-80)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `theme.apply` (Task 1).
- Produces: nothing new — this is the wiring + the human verification gate.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ui.py`:

```python
class TestMainAppliesTheTheme(unittest.TestCase):
    def test_main_calls_theme_apply(self):
        """main() must theme the root BEFORE App builds its widgets — a ttk restyle applied after the
        fact doesn't reach widgets that already read their options."""
        import inspect
        from cas import gui
        src = inspect.getsource(gui.main)
        self.assertIn("theme.apply", src.replace("THEME.apply", "theme.apply"))
        self.assertLess(src.index("apply("), src.index("App("),
                        "theme must be applied before App() builds the widgets")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd tests && python -m unittest test_ui -v`
Expected: FAIL — `AssertionError: 'theme.apply' not found`

- [ ] **Step 3: Apply the theme in `main()`**

Replace `main()` (line ~2365):

```python
def main(adb_bin="adb", fb_bin="fastboot"):
    win = tk.Tk()
    THEME.apply(win)                    # restyle ttk BEFORE the widgets are built
    try:    # GameCove logo in the titlebar/taskbar (keep a ref on win so Tk doesn't GC the image)
        win._cas_icon = tk.PhotoImage(file=str(BUNDLE / "assets" / "cas-window.png"))
        win.iconphoto(True, win._cas_icon)
    except Exception:
        pass
    App(win, adb_bin=adb_bin, fb_bin=fb_bin)
    win.mainloop()
```

(The old `ttk.Style().theme_use("clam")` block goes away — `THEME.apply` does that, and more.)

- [ ] **Step 4: Restyle the tooltip**

Replace `_Tooltip._show` (lines 65-74) — the yellow sticky-note is one of the loudest "old app" tells:

```python
    def _show(self, _e=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)            # no title bar
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left",
                 background=THEME.LIGHT["text"], foreground="#FFFFFF",
                 relief="flat", borderwidth=0, wraplength=380, padx=10, pady=7).pack()
```

- [ ] **Step 5: Run the whole suite**

Run: `cd tests && python -m unittest discover -p "test_*.py"`
Expected: `OK` (411 baseline + the new `test_ui` cases; the display-guarded ones may show as skipped
on a headless box).

- [ ] **Step 6: Launch the real app and look at it**

Run: `python -m cas gui` (or `./scripts/run-gui.sh`)

Check, with at least one device plugged in:
1. The device list fills the window; there is no right-hand panel.
2. Right-click a row → the menu appears, and its Save item is enabled only with one row selected.
3. Ctrl-click a second row → both stay selected; right-click inside the pair → still two selected;
   the footer reads "2 of N devices selected".
4. Right-click a row *outside* the selection → the selection collapses to that one row.
5. `▶ Run` is greyed until both a device is selected and an action is ticked.
6. Tick `① Save` with one device selected, press `▶ Run` → the profile picker appears, the device's
   assigned profile is pre-selected, a profile with a golden shows the replace warning, and `Next ›`
   opens the existing capture picker.
7. Toolbar → `Profiles…` and `Firmware…` open and list the library. Settings → `ES-DE box art…` opens.

Fix anything that misbehaves before committing.

- [ ] **Step 7: Commit**

```bash
git add cas/gui.py tests/test_ui.py
git commit -m "feat(gui): apply the flat theme at startup; dark tooltip chip

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Verification

- [ ] `cd tests && python -m unittest discover -p "test_*.py"` → `OK`, ≥ 411 tests.
- [ ] `bash tests/test_esde_settings.sh` and the other `tests/test_*.sh` still pass (they don't touch
      the GUI, but the CI leg runs them — confirm nothing regressed).
- [ ] `grep -rn "batch_var\|prof_combo\|fw_combo\|_sync_media_tab" cas/` → empty.
- [ ] The app launches and the seven checks in Task 7 Step 6 all pass on the bench.
- [ ] `python -c "import cas.gui, cas.dialogs, cas.theme"` → no import cycle, no third-party import.
