"""Tkinter front-end for CAS. Thin shell over cas.adb / cas.profiles / cas.provision.

Long adb operations run on a background thread; output is funneled to the log pane via a queue so the
UI never freezes. Importing this module is display-safe (Tk() is only created in main()).
"""
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import pathlib
import threading
import datetime
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog, filedialog

from . import APPDIR, BUNDLE, __version__, updater
from . import profiles as P
from . import provision as PV
from . import firmware as FW
from . import warnings as WARN
from . import dialogs as D
from .adb import Adb, Fastboot, list_devices
from . import config
from .config import library_root
from . import theme as THEME


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _profile_library_label(root, reachable):
    """'Library: …' status line for the profile library, with a reachability marker (the drive may be
    unplugged/unmounted)."""
    root = str(root)
    if not reachable:
        return f"Library: {root}   ✗ not reachable (external drive unplugged?)"
    return f"Library: {root}   ✓"


def _lib_watch_action(was, now, busy):
    """Edge decision for the idle library-drive watcher. Given the previously-seen
    reachability (`was`), the current reachability (`now`), and whether a job is running
    (`busy`), return the action to take: 'reconnect' (drive came back — full refresh),
    'disconnect' (drive removed — relabel), 'defer' (came back mid-job — retry later),
    or None (no change)."""
    if now == was:
        return None
    if now:                                  # unreachable → reachable
        return "defer" if busy else "reconnect"
    return "disconnect"                       # reachable → unreachable


class _Tooltip:
    """Minimal hover tooltip (no external deps): a dark chip on <Enter>, gone on <Leave>."""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

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

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def _tip(widget, text):
    """Attach a hover tooltip and return the widget (so it composes inline)."""
    _Tooltip(widget, text)
    return widget


# Friendly display names for the known emulator/frontend packages (with the console they run), so the
# app-select list reads as products, not package ids. Unknown packages fall back to their package id.
_APP_LABELS = {
    "dev.eden.eden_emulator": "Eden  ·  Switch",
    "com.retroarch.aarch64": "RetroArch  ·  multi-system",
    "org.dolphinemu.dolphinemu": "Dolphin  ·  GameCube / Wii",
    "com.flycast.emulator": "Flycast  ·  Dreamcast",
    "com.github.stenzek.duckstation": "DuckStation  ·  PS1",
    "xyz.aethersx2.android": "AetherSX2  ·  PS2",
    "xyz.aethersx2.tturnip": "NetherSX2  ·  PS2",
    "me.magnum.melonds.nightly": "melonDS  ·  DS",
    "org.citra.emu": "Citra  ·  3DS",
    "org.ppsspp.ppsspp": "PPSSPP  ·  PSP",
    "org.mupen64plusae.v3.fzurita": "Mupen64Plus  ·  N64",
    "org.es_de.frontend": "ES-DE  ·  front-end",
    "gamehub.lite": "GameHub  ·  PC games",
    "com.gamecove.gamecove_companion": "GameCove Companion  ·  app",
}

# Behavior @flags shown (as the behavior section) in BOTH app-pick modals — the game frontend's emulator
# picks (@gamelauncher) and the homescreen layout (@homescreen) are behaviors, NOT app rows; the launcher
# packages are system firmware. restore.sh honors each @flag on the device.
_DL_FLAGS = ("settings", "hardening", "grants", "homescreen", "gamelauncher", "wifi")
_DL_FLAG_LABELS = {"settings": "Display & system settings", "hardening": "Performance & update lock",
                   "grants": "Folder permissions", "homescreen": "Homescreen layout",
                   "gamelauncher": "Game launcher emulator picks", "wifi": "WiFi auto-join"}
_DL_FLAG_TIPS = {
    "settings": "Apply the saved display/brightness/animation/screen-timeout preferences.",
    "hardening": "Keep emulators awake (exempt from battery optimization so they're never killed) "
                 "and block OTA system updates that could break root.",
    "grants": "Restore folder-access permissions so ES-DE and the emulators can read your "
              "ROM/BIOS folders without re-asking on first launch.",
    "homescreen": "Restore the homescreen layout — your app folders, icon/dock arrangement, "
                  "wallpaper (and widgets, best-effort). Placed apps missing on the unit are "
                  "installed first so every icon resolves.",
    "gamelauncher": "Save the game frontend's per-system emulator choices (PSX→DuckStation, "
                    "PSP→PPSSPP) and auto-apply them on Download — no manual setup per unit.",
    "wifi": "Clone the golden's saved WiFi so the unit joins it during provisioning (to pull app "
            "and emulator updates). Automatically stripped at Lock, so it ships with no saved network.",
}


def _app_label(pkg):
    """Human-friendly name for a package (falls back to the package id for anything unmapped)."""
    return _APP_LABELS.get(pkg, pkg)


def _manifest_from_axes(axes):
    """Download manifest transform: {pkg:(apk,cfg)} -> (included_pkgs, axes_subset). An app is included
    when EITHER axis is ticked. Pure (no Tk) so it's unit-testable."""
    pkgs = [p for p, (a, c) in axes.items() if a or c]
    return pkgs, {p: axes[p] for p in pkgs}


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
        "seal":            one and online,       # sealing needs adb too — offline just buys a 2-3 min fail
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


class App:
    def __init__(self, win, adb_bin="adb", fb_bin="fastboot"):
        self.win = win
        self.adb_bin = adb_bin
        self.fb_bin = fb_bin
        self.profiles_root = str(library_root())
        self.logq = queue.Queue()
        self.busy = False
        self._tick_id = None        # after() id for the elapsed-time ticker
        self._action = ""           # label of the in-flight action
        self._t0 = 0.0
        self._last_line = ""        # most recent log line — shown live in the status bar
        self._retry_ctx = None      # (message, retry_callable) armed by an op that had failures
        self._icon_refs = []        # live PhotoImage refs so Tk won't GC the app-pick modal's icons
        self.assigned = {}          # serial -> profile name (per-device; remembered across launches)
        self.assigned_manual = set()  # serials whose profile was set by hand (deliberate; allows force)
        self._last_auto = {}        # serial -> last auto-match shown (to detect + announce auto-changes)
        self.fw_resolved = {}       # serial -> firmware.resolve() result dict (for the column + status line)
        self.warnings = []          # warnings.evaluate() result — drives the ⚠ Warnings menu + pre-flight
        self._load_device_profiles()  # restore remembered per-device assignments from cas-config.json
        win.title("CAS — Console Auto Setup")
        win.geometry("1000x720")
        win.minsize(740, 380)       # small but usable: footer + log pinned, the app list scrolls
        self._build_menu()
        self._build()
        self._poll_log()
        self.refresh_profiles()
        self.refresh_devices()
        self._check_updates(manual=False)        # silent startup check; prompts only if newer exists
        self._lib_last_reachable = self._lib_reachable()   # seed the drive-watcher baseline
        self._lib_watch()                                  # idle poll: auto-refresh when the library drive (re)appears

    # ---------- menu bar ----------
    def _build_menu(self):
        bar = tk.Menu(self.win)

        filem = tk.Menu(bar, tearoff=0)
        filem.add_command(label="Refresh devices", command=self.refresh_devices, accelerator="Ctrl+R")
        filem.add_separator()
        filem.add_command(label="Quit", command=self.win.destroy, accelerator="Ctrl+Q")
        bar.add_cascade(label="File", menu=filem)

        setm = tk.Menu(bar, tearoff=0)
        # ONE folder to configure — the CAS Profiles library. Run-history logs and the firmware catalog
        # live UNDER it automatically (config.history_dir / firmware_dir default to library_root()), so
        # there are no separate log/firmware folder pickers to get out of sync.
        setm.add_command(label="Open library folder", command=self._open_library)
        setm.add_command(label="Library folder (CAS Profiles)…", command=self.choose_library)
        setm.add_command(label="Managed APKs…", command=self._open_apk_store)
        setm.add_command(label="Run history…", command=self._open_history)
        setm.add_command(label="ES-DE box art…", command=self._open_boxart)
        setm.add_separator()
        setm.add_command(label="Seal selected unit (retail lock)…", command=self.seal_selected)
        setm.add_command(label="Release selected unit (un-provision)…", command=self.release_selected)
        bar.add_cascade(label="Settings", menu=setm)

        # Live "⚠ Warnings (N)" cascade — its label + submenu are rebuilt on every refresh via
        # _rebuild_warnings_menu(). We keep the bar + submenu handles and the cascade's index to relabel it.
        self._menubar = bar
        self._warn_menu = tk.Menu(bar, tearoff=0)
        bar.add_cascade(label="✓ Warnings", menu=self._warn_menu)
        self._warn_index = bar.index("end")

        helpm = tk.Menu(bar, tearoff=0)
        helpm.add_command(label="Check for updates…", command=lambda: self._check_updates(manual=True))
        helpm.add_command(label="About CAS", command=self._about)
        bar.add_cascade(label="Help", menu=helpm)

        self.win.config(menu=bar)
        self.win.bind_all("<Control-r>", lambda e: self.refresh_devices())
        self.win.bind_all("<Control-q>", lambda e: self.win.destroy())

    # ---------- self-update (GitHub Release; runtime siblings stay external) ----------
    def _check_updates(self, manual=False):
        """Background check against the public GitHub Release. manual=True also reports 'up to date'.

        In-app self-update only applies to a FROZEN release bundle (cas-gui[.exe]). Running from a source
        checkout (`python -m cas`) IS already your latest local code, and the swap can't run there — so we
        never nag on startup from source, and a MANUAL check just points at git pull (update.sh /
        update-win.bat). This is why a source run used to pop a bogus 'new update available'."""
        if not getattr(sys, "frozen", False):
            if manual:
                messagebox.showinfo(
                    "CAS",
                    f"Running from source (v{__version__}) — this is your local code, already up to date "
                    "with your working tree.\n\nIn-app update only applies to the packaged build; to update "
                    "a source checkout use git pull (or update.sh / update-win.bat).")
            return
        def work():
            up = updater.check(__version__)
            self.win.after(0, lambda: self._on_update_result(up, manual))
        threading.Thread(target=work, daemon=True).start()

    def _on_update_result(self, up, manual):
        if not up:
            if manual:
                messagebox.showinfo("CAS", f"You're on the latest version (v{__version__}).")
            return
        if messagebox.askyesno(
                "Update available",
                f"CAS v{up['version']} is available (you have v{__version__}).\n\n"
                f"{up.get('notes', '')}\n\nDownload it and restart CAS now?"):
            self._apply_update(up)

    def _apply_update(self, up):
        """Download + apply the update behind a MODAL, non-cancelable progress dialog. While it's up the
        operator can't touch the main window (grab_set) — no half-provisioned device from a stray click
        mid-swap — and a live progress bar shows the download isn't frozen. Download AND staging run on a
        worker thread so the dialog keeps animating; only success closes the app (the helper relaunches)."""
        self.log(f"downloading CAS v{up['version']} …")
        dest = str(pathlib.Path(tempfile.gettempdir()) / "cas-update.zip")

        dlg = tk.Toplevel(self.win)
        dlg.title("Updating CAS")
        dlg.transient(self.win)
        dlg.resizable(False, False)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)        # no close box while a swap is in flight
        frm = ttk.Frame(dlg, padding=18)
        frm.pack(fill="both", expand=True)
        head = ttk.Label(frm, text=f"Downloading CAS v{up['version']} …",
                         font=("TkDefaultFont", 11, "bold"))
        head.pack(anchor="w")
        subl = ttk.Label(frm, text="Please wait — don't unplug devices or close CAS.",
                         foreground="#555")
        subl.pack(anchor="w", pady=(2, 10))
        bar = ttk.Progressbar(frm, mode="determinate", maximum=100, length=380)
        bar.pack(fill="x")
        stat = ttk.Label(frm, text="Starting…", foreground="#555")
        stat.pack(anchor="w", pady=(8, 0))
        btnrow = ttk.Frame(frm)
        btnrow.pack(anchor="e", pady=(12, 0))
        closeb = ttk.Button(btnrow, text="Close", command=dlg.destroy)   # shown only if it fails

        dlg.update_idletasks()
        try:                                                   # center over the main window
            x = self.win.winfo_rootx() + (self.win.winfo_width() - dlg.winfo_width()) // 2
            y = self.win.winfo_rooty() + 110
            dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass
        dlg.grab_set()                                         # MODAL: blocks the main window entirely

        state = {"pct": -1, "mode": "determinate"}

        def fail(msg):
            if str(bar["mode"]) == "indeterminate":
                bar.stop()
            head.config(text="Update failed")
            subl.config(text="The current version is unchanged. You can close this and try again later.")
            stat.config(text=msg)
            closeb.pack(side="right")                          # let them dismiss + retry later
            dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)

        def on_progress(done, total):                          # called on the worker thread
            pct = int(done * 100 / total) if total else 0
            def ui():
                if total:
                    if state["mode"] != "determinate":
                        bar.stop(); bar.config(mode="determinate"); state["mode"] = "determinate"
                    bar["value"] = pct
                    stat.config(text=f"{done/1048576:.1f} / {total/1048576:.1f} MB   ({pct}%)")
                else:                                          # server sent no size → march indeterminately
                    if state["mode"] != "indeterminate":
                        bar.config(mode="indeterminate"); bar.start(12); state["mode"] = "indeterminate"
                    stat.config(text=f"{done/1048576:.1f} MB")
            if not total or pct != state["pct"]:               # throttle determinate redraws to 1%/step
                state["pct"] = pct
                self.win.after(0, ui)

        def staged_ok():
            if str(bar["mode"]) == "indeterminate":
                bar.stop()
            bar.config(mode="determinate"); bar["value"] = 100
            stat.config(text="Update staged — CAS will restart now.")
            dlg.update_idletasks()
            self.win.after(700, self.win.destroy)              # quit; the helper swaps + relaunches the new build

        def do_stage():
            head.config(text=f"Applying CAS v{up['version']} …")
            bar.stop(); bar.config(mode="indeterminate"); bar.start(12); state["mode"] = "indeterminate"
            stat.config(text="Swapping in the new version…")
            def work_stage():
                ok = updater.stage_and_relaunch(dest, appdir=APPDIR, log=self.log)
                self.win.after(0, staged_ok if ok else lambda: fail(
                    "Couldn't apply the update — see cas-update.log next to the app."))
            threading.Thread(target=work_stage, daemon=True).start()

        def work_download():
            ok = updater.download_and_verify(up["url"], dest, up.get("sha256", ""), progress=on_progress)
            self.win.after(0, do_stage if ok else lambda: fail(
                "Download or checksum check failed — not updating."))
        threading.Thread(target=work_download, daemon=True).start()

    def _open_library(self):
        """Open the active library folder in the OS file manager."""
        target = str(self.profiles_root)
        if not self._open_path(target):
            messagebox.showwarning(
                "CAS",
                f"Couldn't open a file manager for:\n{target}\n\n"
                "Open it manually in your file manager (paste the path above).")

    def choose_library(self):
        """Pick the profile/golden library folder — e.g. the external drive '…/CAS Profiles' so goldens are
        shared across benches too. Cancel offers to CLEAR the override so the library falls back to the local
        default. Re-resolves profiles, firmware and devices after."""
        def _applied():
            self.profiles_root = str(library_root())
            self._lib_last_reachable = self._lib_reachable()   # re-baseline: a path change is not a drive edge
            self._update_lib_label()
            self.refresh_profiles()
            self.refresh_devices()
        cur = config.load_config().get("library")
        d = filedialog.askdirectory(
            title="Profile/golden library folder — e.g. the external drive '…/CAS Profiles'  (Cancel to clear)",
            initialdir=(cur or str(APPDIR / "data")))
        if d:
            config.set_library(d)
            _applied()
            self.log(f"Library → {d}")
        elif cur and messagebox.askyesno(
                "CAS", "Clear the library override? The library falls back to the local default "
                       "(APPDIR/data/profiles)."):
            config.set_library(None)
            _applied()
            self.log(f"Library override cleared → {self.profiles_root}")

    def _open_path(self, target):
        """Open a local folder path in the OS file manager. Returns True if a viewer was launched."""
        try:
            if sys.platform == "win32":
                os.startfile(target)                     # noqa: Explorer handles local paths
                return True
            if sys.platform == "darwin":
                subprocess.Popen(["open", target])
                return True
            order = ["xdg-open", "dolphin", "nautilus", "nemo", "caja", "pcmanfm-qt", "pcmanfm", "thunar"]
            for fm in order:
                if shutil.which(fm):
                    subprocess.Popen([fm, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
            return False
        except Exception as e:
            messagebox.showerror("CAS", f"Could not open:\n{target}\n\n{e}")
            return False

    def _about(self):
        from .config import load_config
        p = str(self.profiles_root)
        if os.environ.get("CAS_PROFILES") or load_config().get("library"):
            where = "configured library"
        elif p == str(APPDIR / "data" / "profiles"):
            where = "local default"
        else:
            where = ""
        reach = "reachable ✓" if self._lib_reachable() else "not reachable ✗"
        adb_disp = self.adb_bin if ("/" in self.adb_bin or "\\" in self.adb_bin) else f"{self.adb_bin}  (system PATH)"

        dlg = tk.Toplevel(self.win)
        dlg.title("About CAS")
        dlg.transient(self.win)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=18)
        frm.pack(fill="both", expand=True)
        try:                                            # GameCove logo (optional — skip if asset missing)
            logo = tk.PhotoImage(file=str(BUNDLE / "assets" / "cas-window.png"))
            logo = logo.subsample(max(1, logo.width() // 64), max(1, logo.height() // 64))
            lb = ttk.Label(frm, image=logo); lb.image = logo     # keep a ref so Tk doesn't GC it
            lb.grid(row=0, column=0, rowspan=2, padx=(0, 16), sticky="n")
        except Exception:
            pass
        ttk.Label(frm, text="CAS — Console Auto Setup",
                  font=("TkDefaultFont", 13, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(frm, text=f"v{__version__}  ·  GameCove handheld provisioning toolkit",
                  foreground="#666").grid(row=1, column=1, sticky="w", pady=(2, 0))
        info = ttk.Frame(frm)
        info.grid(row=2, column=0, columnspan=2, sticky="we", pady=(16, 12))

        def _row(r, k, v):
            ttk.Label(info, text=k, font=("TkDefaultFont", 9, "bold")).grid(row=r, column=0, sticky="nw", padx=(0, 10))
            ttk.Label(info, text=v, wraplength=380, justify="left").grid(row=r, column=1, sticky="w")
        _row(0, "Library", p)
        _row(1, "", f"{where}  ·  {reach}")
        _row(2, "adb", adb_disp)

        ttk.Button(frm, text="Close", command=dlg.destroy).grid(row=3, column=0, columnspan=2)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        try:                                            # center over the main window
            x = self.win.winfo_rootx() + (self.win.winfo_width() - dlg.winfo_width()) // 2
            y = self.win.winfo_rooty() + 90
            dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass
        dlg.grab_set()

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
        THEME.center_columns(self.dev_tree)
        vsb = ttk.Scrollbar(listf, orient="vertical", command=self.dev_tree.yview)
        self.dev_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.dev_tree.pack(side="left", fill="both", expand=True)
        self.dev_tree.bind("<Double-1>", self._assign_on_doubleclick)
        self.dev_tree.bind("<<TreeviewSelect>>", lambda e: self._on_tree_select())
        # Select-all works from ANYWHERE on the window (the hint line advertises it unconditionally), not
        # only when the device list has focus — plus ⌘A on macOS. The tree keeps its own binding so a
        # "break" there stops the window binding from firing a second time when the tree is focused.
        self.dev_tree.bind("<Control-a>", self._select_all_shortcut)
        self.win.bind("<Control-a>", self._select_all_shortcut)
        self.win.bind("<Command-a>", self._select_all_shortcut)
        self._build_context_menu()

        # Box-art state lives on the App (BoxArtDialog binds to these); no widget in the main window.
        self.media_mode = tk.StringVar(value="push" if config.es_media_src() else "sd")
        self.media_path = tk.StringVar(value=config.es_media_src() or "")
        self.sd_media_var = tk.StringVar(value="")

        self._sync_save_selection_gate()      # nothing selected yet → ① Save starts disabled
        self._update_run_state()

    def _on_tree_select(self):
        self.sel_var.set(_selection_summary(len(self.dev_tree.selection()),
                                            len(self.dev_tree.get_children())))
        self._sync_save_selection_gate()        # selection changed → re-check the ① Save single-device gate
        self._update_run_state()

    def _sync_save_selection_gate(self):
        """① Save captures ONE golden device: its checkbox is enabled only when exactly one row is
        selected, and gets force-unticked the moment that stops being true (e.g. the operator ticks Save
        with one row selected, then Ctrl-clicks to add another). Called on every selection change AND
        every chain-checkbox tick, so the checkbox's live state can never promise something ▶ Run would
        actually refuse (BLOCKING FINDING 1: the footer said '3 of 4 devices selected' while Save silently
        ran on just the topmost one). This is the proactive half of the fix; run_chain() keeps its own
        guard as defense-in-depth in case the selection changes between a tick and the click."""
        if any(self.chain_vars[k].get() for k in ("download", "warmup", "lock")):
            return                                  # _on_chain_tick already owns Save's state in this case
        one_selected = len(self.dev_tree.selection()) == 1
        self.chain_cbs["save"].configure(state="normal" if one_selected else "disabled")
        if not one_selected and self.chain_vars["save"].get():
            self.chain_vars["save"].set(False)

    def _update_run_state(self):
        """▶ Run is live only when something is selected AND at least one action is ticked — instead of
        popping a 'select a device' box AFTER the click."""
        if self.busy:
            return                                   # _run_bg owns the button while a job is in flight
        ready = bool(self.dev_tree.selection()) and any(v.get() for v in self.chain_vars.values())
        self.run_btn.configure(state="normal" if ready else "disabled")

    def clear_selection(self):
        self.dev_tree.selection_remove(*self.dev_tree.selection())

    # ---------- logging / threading ----------
    def log(self, msg):
        self.logq.put(str(msg))

    def _poll_log(self):
        wrote = False
        last = None
        while not self.logq.empty():
            line = self.logq.get()
            last = line
            if not wrote:
                self.logbox.configure(state="normal")
                wrote = True
            self.logbox.insert("end", line + "\n")
            self._maybe_progress(line)        # drive the % bar from adb transfer lines
        if wrote:
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        if last is not None and self.busy:
            self._last_line = last            # surface the newest activity line in the status bar
        self.win.after(150, self._poll_log)

    _PCT = re.compile(r"\[\s*(\d+)%\]")     # adb pull/push progress, e.g. '[ 42%] /data/...'

    def _maybe_progress(self, line):
        """If a streamed line carries an adb transfer percent, switch the bar to a real % fill."""
        m = self._PCT.search(line)
        if not m:
            return
        pct = max(0, min(100, int(m.group(1))))
        if str(self.progress["mode"]) != "determinate":
            self.progress.stop()
            self.progress.config(mode="determinate", maximum=100)
        self.progress["value"] = pct

    def _tick(self):
        """Update the status once a second while a job runs: elapsed time + what it's doing right now."""
        if not self.busy:
            return
        el = int(time.monotonic() - self._t0)
        what = self._last_line or self._action
        if len(what) > 64:
            what = what[:61] + "..."
        self.status_var.set(f"⏳ {el // 60}m {el % 60:02d}s — {what}")
        self._tick_id = self.win.after(1000, self._tick)

    def _run_bg(self, fn, label="Working"):
        if self.busy:
            self.log("busy — wait for the current operation to finish.")
            return
        self.busy = True
        self._action = label
        self._t0 = time.monotonic()
        self.cancel_event = threading.Event()       # this op's abort signal (set by the Cancel button)
        self._flash_critical = False                # True only during the init_boot WRITE (brick-warning gate)
        self.log(f"⏱ starting: {label}")
        for b in self.btns:
            b.configure(state="disabled")
        self.cancel_btn.configure(state="normal", text="✗ Cancel")
        self._last_line = ""
        self.progress.config(mode="indeterminate")     # default: marching bar until a real % arrives
        self.progress["value"] = 0
        self.progress.start(15)        # animated marching bar = "I'm working"
        self._tick()
        try:
            self.win.configure(cursor="watch")
        except tk.TclError:
            pass

        result_box = {"r": None}

        def done():
            self.busy = False
            if self._tick_id:
                self.win.after_cancel(self._tick_id)
                self._tick_id = None
            self.progress.stop()
            self.progress.config(mode="indeterminate")     # reset for the next run
            self.progress["value"] = 0
            el = int(time.monotonic() - self._t0)
            self.status_var.set(f"Ready.  (last action took {el // 60}m {el % 60:02d}s)")
            self.log(f"⏱ {self._action} — finished in {el // 60}m {el % 60:02d}s")
            # Restore the controls BEFORE the report/retry prompt — those run after, so an exception in
            # either must never be able to leave the buttons greyed and the watch cursor stuck ("loading").
            self.cancel_btn.configure(state="disabled", text="✗ Cancel")
            self.cancel_event = None
            self._flash_critical = False
            for b in self.btns:
                b.configure(state="normal")
            self._update_run_state()                 # ← an empty selection leaves ▶ Run disabled
            try:
                self.win.configure(cursor="")
            except tk.TclError:
                pass
            self._report(self._action, result_box.get("r"))
            # if the op armed a retry (some devices failed), offer to re-run JUST those now that we're idle.
            ctx = self._retry_ctx
            self._retry_ctx = None
            if ctx:
                msg, again = ctx
                if messagebox.askyesno("CAS — retry the failures?", msg):
                    again()

        def wrap():
            try:
                result_box["r"] = fn()
            except Exception as e:  # surface any error to the log instead of dying silently
                self.log(f"ERROR: {e}")
                result_box["r"] = False
            finally:
                self.win.after(0, done)
        threading.Thread(target=wrap, daemon=True).start()

    def _cancel_op(self):
        """Abort the running operation. During the init_boot WRITE, confirm first (a mid-flash cancel can
        brick). Otherwise signal immediately — the subprocess / stream / wait layers stop within ~1 s."""
        ev = getattr(self, "cancel_event", None)
        if ev is None or not self.busy:
            return
        if getattr(self, "_flash_critical", False):
            if not messagebox.askyesno(
                    "CAS — cancel during a flash?",
                    "A device is being FLASHED right now. Interrupting a flash can BRICK the unit.\n\n"
                    "Cancel anyway?"):
                return
        ev.set()
        self.log("⏹ cancelling — stopping the current operation…")
        self.cancel_btn.configure(state="disabled", text="cancelling…")

    def _on_flash_critical(self, active):
        """Called from a worker thread by the flash backends around the partition write; marshal the flag to
        the UI thread so _cancel_op knows whether to show the brick-warning before aborting."""
        self.win.after(0, lambda a=bool(active): setattr(self, "_flash_critical", a))

    def _report(self, label, result):
        """Post-action mini-report in the log — a clear pass / skip / fail summary for easy debugging.
        Accepts a batch dict {serial: (status, detail)}, a single bool, or None (nothing to summarize)."""
        DONE = ("ok",)
        SKIP = ("skip", "skip-golden", "no-profile", "no-init_boot")
        CANCEL = ("cancelled",)
        if isinstance(result, dict):
            # Normalise each value to (status, detail). Tolerate a bare status or a short/long tuple so a
            # malformed result can never raise here — _report runs inside done(), and a crash there used to
            # leave the controls disabled and the watch cursor stuck ("done saving but still loading").
            def _split(v):
                if isinstance(v, (tuple, list)):
                    return (v[0] if v else "", v[1] if len(v) > 1 else "")
                return (v, "")
            norm = {s: _split(v) for s, v in result.items()}
            good = [s for s, (st, _) in norm.items() if st in DONE]
            skipped = [s for s, (st, _) in norm.items() if st in SKIP]
            cancelled = [s for s, (st, _) in norm.items() if st in CANCEL]
            bad = [s for s, (st, _) in norm.items()
                   if st not in DONE and st not in SKIP and st not in CANCEL]
            self.log(f"──────── REPORT: {label} ────────")
            for s, (st, d) in norm.items():
                mark = ("✅" if st in DONE else "⏭" if st in SKIP else "⏹" if st in CANCEL else "❌")
                self.log(f"   {mark} {s}: {st}" + (f" — {d}" if d else ""))
            summary = f"   → {len(good)} ok"
            if skipped:
                summary += f", {len(skipped)} skipped"
            if cancelled:
                summary += f", {len(cancelled)} cancelled"
            summary += f", {len(bad)} failed"
            if bad:
                summary += f"   ·   FAILED: {', '.join(bad)}"
            self.log(summary)
            self.log("────────────────────────────────")
        elif result is True:
            self.log(f"✅ REPORT: {label} — SUCCESS.")
        elif result is False:
            self.log(f"❌ REPORT: {label} — FAILED. Check the steps logged above for the failing point.")

    # ---------- data refresh ----------
    def refresh_profiles(self):
        """Re-read the library (the profile windows/pickers list it live, so there's no combobox to
        fill any more) and warn when the library drive isn't there."""
        self._update_lib_label()
        if not self._lib_reachable():
            self.log(f"Library not reachable: {self.profiles_root} — is the library drive connected? "
                     "Use Settings → 'Library folder…' to fix the path.")

    def refresh_devices(self):
        self.dev_tree.delete(*self.dev_tree.get_children())

        def work():
            try:
                devs = list_devices(adb=self.adb_bin)
            except Exception as e:
                self.log(f"adb error: {e} (is adb on PATH?)")
                return
            rows = []
            fwmap = {}                                   # serial -> firmware.resolve() result
            snaps = {}                                   # serial -> device-side snapshot for warnings.evaluate
            try:
                fw_root = FW.firmware_root()
            except Exception:
                fw_root = None
            for serial, state in devs:
                model, sd, auto = "", "", ""
                snap = {"serial": serial, "state": state, "model": "", "identity": {},
                        "fw": {}, "bootloader": "unknown"}
                if state == "device":
                    a = Adb(serial=serial, adb=self.adb_bin)
                    model = a.getprop("ro.product.model")
                    try:
                        sd = a.sd_info()        # SD serial + size (or 'no SD') — catches wrong/missing cards
                    except Exception:
                        sd = "?"
                    # auto-match by model NAME similarity, with the SD size choosing the capacity tier
                    m = P.match_profile(model, self.profiles_root, sd_gb=P.parse_sd_gb(sd))
                    auto = m.name if m else "(no match)"
                    # device-root-firmware: suggest by serial-prefix/props (sticky manual override wins),
                    # then logic-check vs the live partition scheme. Library-only — never flashes.
                    try:
                        idn = FW.identity(a)
                    except Exception:
                        idn = {}
                    if fw_root is not None:
                        try:
                            fwmap[serial] = FW.resolve(serial, idn, fw_root)
                        except Exception as e:
                            fwmap[serial] = {"firmware_id": None, "version": None, "manual": False,
                                             "ok": False, "warnings": [f"error: {e}"], "firmware": None}
                    try:
                        boot = a.bootloader_state()
                    except Exception:
                        boot = "unknown"
                    snap.update(model=model, identity=idn, fw=fwmap.get(serial, {}), bootloader=boot)
                snaps[serial] = snap
                rows.append((serial, model, sd, auto, state))
            self.win.after(0, lambda r=rows, fm=fwmap, sn=snaps: self._populate_devices(r, fm, sn))
        threading.Thread(target=work, daemon=True).start()

    def _load_device_profiles(self):
        """Restore remembered per-device assignments (by adb serial) from cas-config.json, keeping only
        those whose profile still exists in the library. Manual ones are tinted; both kinds are sticky."""
        try:
            valid = {p.name for p in P.list_profiles(self.profiles_root)}
        except Exception:
            valid = set()
        for serial, rec in config.get_device_profiles().items():
            if rec["profile"] in valid:
                self.assigned[serial] = rec["profile"]
                if rec["manual"]:
                    self.assigned_manual.add(serial)

    def _populate_devices(self, rows, fwmap=None, snaps=None):
        """(UI thread) fill the device tree (keyed by adb serial):
          * MANUAL assignment  -> sticky + remembered across launches; the operator's choice always wins.
          * AUTO assignment    -> FOLLOWS the live match every refresh, so if something CHANGES (SD card
                                  swapped to a different tier, a better-matching profile added) the device
                                  re-auto-assigns itself.
        Hand-assigned rows are marked '(pinned)' in the profile column (row colour now carries device
        state instead)."""
        self.fw_resolved = fwmap or {}
        snaps = snaps or {}
        self.dev_tree.delete(*self.dev_tree.get_children())
        changes = []                                       # auto-matches that CHANGED for a known device
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
        self._evaluate_warnings(snaps)
        self.log("refreshed: " + ", ".join(f"{r[0]} → {self.assigned.get(r[0], '?')}" for r in rows)
                 if rows else "refreshed: 0 device(s)")
        if changes:                                        # inform the operator an auto-match changed (e.g. SD swap)
            body = "\n".join(f"  {s}:  {old}  →  {new}" for s, old, new in changes)
            messagebox.showinfo(
                "CAS — auto-match updated",
                f"A device's auto-matched profile changed (SD card or library change):\n\n{body}\n\n"
                "Kept automatically. Right-click the row → Assign profile (or double-click) to override.")

    # ---------- warnings (⚠ Warnings menu + pre-flight gating) ----------
    def _profile_facts(self, name, model):
        """(has_golden, model_match_ok) for the EFFECTIVE assigned profile, used to derive warnings.
        Returns (None, None) when no profile is assigned; model_match_ok is None when the profile sets no
        model_match (or the model is unknown) — only False (a real mismatch) raises a warning."""
        if not name or name == "(no match)":
            return None, None
        try:
            prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
            gold = prof.has_golden()
            mm = prof.meta.get("model_match")
            mm_ok = None if not (mm and model) else bool(re.search(mm, model))
        except Exception:
            return None, None
        return gold, mm_ok

    def _evaluate_warnings(self, snaps):
        """Recompute self.warnings from the device snapshots + global state, then rebuild the menu."""
        gstate = {"library_reachable": self._lib_reachable()}
        try:
            gstate["firmware_library_empty"] = not FW.list_firmware(FW.firmware_root())
        except Exception:
            gstate["firmware_library_empty"] = False
        try:
            self.warnings = WARN.evaluate(list(snaps.values()), gstate)
        except Exception as e:
            self.warnings = []
            self.log(f"warning-eval error: {e}")
        self._rebuild_warnings_menu()

    _SEV_ICON = {"block": "✗", "confirm": "⚠", "info": "ℹ"}

    def _rebuild_warnings_menu(self):
        """Relabel the top-level cascade ('✓ Warnings' / '⚠ Warnings (N)') and repopulate its submenu —
        one click-to-select entry per warning, info grouped below, then 'Open warnings report…'."""
        m = self._warn_menu
        m.delete(0, "end")
        actionable = [w for w in self.warnings if w["severity"] in ("block", "confirm")]
        info = [w for w in self.warnings if w["severity"] == "info"]
        label = f"⚠ Warnings ({len(actionable)})" if actionable else (
                "ℹ Warnings" if info else "✓ Warnings")
        try:
            self._menubar.entryconfig(self._warn_index, label=label)
        except tk.TclError:
            pass
        if not self.warnings:
            m.add_command(label="No warnings — all clear", state="disabled")
            return
        for w in actionable + info:
            who = w["serial"] or "ALL"
            m.add_command(label=f"{self._SEV_ICON[w['severity']]}  {who} — {w['title']}",
                          command=lambda ww=w: self._warning_clicked(ww))
        m.add_separator()
        m.add_command(label="Open warnings report…", command=self._open_warnings_report)

    def _warning_clicked(self, w):
        """Select the offending device row (if any) and show the warning's detail + suggested fix."""
        s = w.get("serial")
        if s and self.dev_tree.exists(s):
            self.dev_tree.selection_set(s)
            self.dev_tree.see(s)
            self.dev_tree.focus(s)
        scope = "All devices" if w["scope"] == "global" else (s or "device")
        messagebox.showinfo(f"CAS — {self._SEV_ICON[w['severity']]} {w['title']}",
                            f"{scope}\n\n{w['detail']}\n\nWhat to do:\n{w['fix']}")

    def _open_warnings_report(self):
        """A grouped, color-coded report of every current warning, with a Copy-to-clipboard button."""
        dlg = tk.Toplevel(self.win)
        dlg.title("CAS — warnings report")
        dlg.geometry("680x420")
        tree = ttk.Treeview(dlg, columns=("sev", "what", "fix"), show="tree headings")
        tree.heading("#0", text="device"); tree.column("#0", width=130, anchor="w")
        tree.heading("sev", text=""); tree.column("sev", width=34, anchor="center")
        tree.heading("what", text="warning"); tree.column("what", width=260, anchor="w")
        tree.heading("fix", text="what to do"); tree.column("fix", width=240, anchor="w")
        for sev, color in (("block", "#b00020"), ("confirm", "#a06000"), ("info", "#666666")):
            tree.tag_configure(sev, foreground=color)
        groups, lines = {}, []
        for w in self.warnings:
            who = "All devices" if w["scope"] == "global" else (w["serial"] or "device")
            if who not in groups:
                groups[who] = tree.insert("", "end", text=who, open=True)
            tree.insert(groups[who], "end", text="",
                        values=(self._SEV_ICON[w["severity"]], w["title"], w["fix"]), tags=(w["severity"],))
            lines.append(f"[{w['severity']}] {who}: {w['title']} — {w['fix']}")
        if not self.warnings:
            tree.insert("", "end", text="✓ all clear", values=("", "no warnings", ""))
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        bar = ttk.Frame(dlg); bar.pack(fill="x", padx=8, pady=(0, 8))

        def copy():
            self.win.clipboard_clear()
            self.win.clipboard_append("\n".join(lines) or "no warnings")
            self.log("warnings report copied to clipboard.")
        ttk.Button(bar, text="Copy", command=copy).pack(side="right")
        ttk.Button(bar, text="Close", command=dlg.destroy).pack(side="right", padx=(0, 6))

    def _preflight(self, actions, serials):
        """Gate `serials` against the current warnings for `actions` (UI thread, before launching work).
        Global blockers abort the whole op; per-device hard blocks are skipped (logged); soft warnings ask
        'proceed anyway?'. Returns the surviving serials, or None to abort entirely."""
        gb = WARN.gate(self.warnings, None, actions)["block"]
        if gb:
            messagebox.showerror("CAS — can't run",
                                 "Resolve these first:\n\n" + "\n".join(f"• {w['title']}\n   {w['fix']}"
                                                                        for w in gb))
            return None
        cleared = []
        for s in serials:
            g = WARN.gate(self.warnings, s, actions)
            if g["block"]:
                self.log(f"⏭ skipped {s}: " + "; ".join(w["title"] for w in g["block"]))
                continue
            if g["confirm"]:
                if not messagebox.askyesno(
                        f"CAS — proceed on {s}?",
                        f"{s} has warnings:\n\n" + "\n".join(f"⚠ {w['title']}\n   {w['fix']}"
                                                             for w in g["confirm"])
                        + "\n\nProceed on this device anyway?"):
                    self.log(f"⏭ skipped {s}: operator declined the warning(s).")
                    continue
            cleared.append(s)
        if not cleared:
            messagebox.showinfo("CAS", "Nothing to run — every targeted device was skipped or blocked.")
            return None
        return cleared

    # ---------- actions ----------
    def _selected_serial(self):
        sel = self.dev_tree.selection()
        return sel[0] if sel else None

    def _scan_device_apps(self, serial):
        """Third-party packages on the connected device (pm list -3), sorted. [] if no device/scan fails."""
        if not serial:
            return []
        rc, out, _ = Adb(serial=serial, adb=self.adb_bin).shell("pm list packages -3")
        if rc != 0:
            return []
        return sorted(l.split("package:", 1)[1].strip()
                      for l in out.splitlines() if l.startswith("package:"))

    def _detect_device_launchers(self, serial):
        """(game_launcher, home_launcher) on the device, or None for either it can't resolve. Uses the SAME
        lib-root.sh functions the capture engine uses — but PUSHED here first, because the Save modal opens
        BEFORE capture_to_pc pushes the scripts; without this the source failed and both came back None
        (so the homescreen / game-launcher rows never appeared). home resolves WITHOUT root (cmd package);
        the game frontend prefers root (signature-probe /data/data) but falls back to the curated list
        under a plain shell when su isn't granted yet."""
        if not serial:
            return (None, None)
        a = Adb(serial=serial, adb=self.adb_bin)
        a.shell("mkdir -p /data/local/tmp/cas_scripts")
        if not a.push(PV.LIBROOT, "/data/local/tmp/cas_scripts/"):    # must be present to source
            return (None, None)
        src = ". /data/local/tmp/cas_scripts/lib-root.sh 2>/dev/null; "

        def _last(rc, out):
            line = (out or "").strip().splitlines()
            return line[-1].strip() if rc == 0 and line and line[-1].strip() else None
        rc, out, _ = a.shell(src + "home_launcher")                               # no root needed
        home = _last(rc, out)
        # short timeout: detection is best-effort and runs on the UI thread, so a pending su-grant prompt
        # must NOT freeze the Save modal for the default 15 min — fail fast and fall back to the shell probe.
        rc, out, _ = a.su(src + "game_launcher", timeout=20)                       # root: /data/data probe
        game = _last(rc, out)
        if game is None:                                                           # su blocked → curated list
            rc, out, _ = a.shell(src + "game_launcher")
            game = _last(rc, out)
        return (game, home)

    def _set_all(self, vars_dict, value, launchers=frozenset(), cfg_disabled=frozenset()):
        """Set every (apk_var, cfg_var) pair in vars_dict to value. Launcher rows keep their (disabled)
        APK box untouched, and cfg_disabled rows keep their (disabled) Config box untouched, so neither
        diverges from the on-screen state. Used by the modal's Select all / Deselect all buttons."""
        for pkg, (apk_v, cfg_v) in vars_dict.items():
            if pkg not in launchers:
                apk_v.set(value)
            if pkg not in cfg_disabled:
                cfg_v.set(value)

    # ── Run-time app picker ───────────────────────────────────────────────────────────────────────────
    # App selection is no longer a sidebar list; it pops here when ▶ Run needs it. One reusable modal,
    # two thin wrappers: Save scans the connected device, Download lists the golden. Each wrapper writes
    # the same manifest files the old "Save … selection" buttons did, then the chain runs.

    def _app_pick_modal(self, title, intro, prof, rows, launchers, flag_specs, labels=None,
                        flags_caption="— behavior —", cfg_disabled=None, apk_locked=None):
        """Modal app picker. `rows` is an ordered {pkg:(apk0,cfg0)} initial tick state; `launchers` is the
        set of pkgs whose APK box is disabled (system firmware, never reinstalled); `cfg_disabled` is the
        set of pkgs whose Config box is disabled (nothing was captured to restore); `flag_specs` is an
        ordered list of (key, label, tip, initial_bool) for the behavior block (captioned by
        `flags_caption`); `labels` is an optional {pkg: friendly_name} override (used to give the launcher
        rows a role label). Blocks until the operator clicks Run or Cancel. Returns (axes, flags) on Run —
        axes={pkg:(apk,cfg)} for every row, flags={key:'on'/'off'} for every flag_spec — or None on
        Cancel/close."""
        labels = labels or {}
        cfg_disabled = cfg_disabled or set()
        apk_locked = apk_locked or set()                   # always-install pkgs: APK forced-on + disabled
        self._icon_refs = []                               # fresh icon refs for this modal's lifetime
        win = tk.Toplevel(self.win)
        win.title(title)
        win.transient(self.win)
        win.grab_set()
        win.minsize(480, 420)
        result = {}                                        # filled on Run; stays empty on Cancel
        pick_vars, flag_vars = {}, {}

        ttk.Label(win, text=intro, wraplength=470, justify="left", foreground="#555") \
            .pack(side="top", anchor="w", padx=10, pady=(10, 4))

        btnrow = ttk.Frame(win, padding=(10, 6))           # pinned at the bottom
        btnrow.pack(side="bottom", fill="x")

        def _run():
            result["axes"] = {p: (a.get(), c.get()) for p, (a, c) in pick_vars.items()}
            result["flags"] = {k: ("on" if v.get() else "off") for k, v in flag_vars.items()}
            win.destroy()
        _tip(ttk.Button(btnrow, text="▶ Run", command=_run),
             "Save this selection and run the action.").pack(side="right")
        ttk.Button(btnrow, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 6))

        selrow = ttk.Frame(win, padding=(10, 0))
        selrow.pack(side="top", anchor="w")
        ttk.Button(selrow, text="Select all",
                   command=lambda: self._set_all(pick_vars, True, launchers | apk_locked, cfg_disabled)) \
            .pack(side="left")
        ttk.Button(selrow, text="Deselect all",
                   command=lambda: self._set_all(pick_vars, False, launchers | apk_locked, cfg_disabled)) \
            .pack(side="left", padx=(4, 0))

        bodywrap = ttk.Frame(win, padding=(6, 4))
        bodywrap.pack(side="top", fill="both", expand=True)
        listf = self._scroll_tab(bodywrap)                 # scrollable inner frame for the app rows
        for pkg, (apk0, cfg0) in rows.items():
            is_launcher = pkg in launchers
            cfg_off = pkg in cfg_disabled
            if is_launcher:
                apk0 = False                               # system firmware — never reinstalled
            if cfg_off:
                cfg0 = False                               # nothing captured -> can't restore config
            apk_v, cfg_v = tk.BooleanVar(value=apk0), tk.BooleanVar(value=cfg0)
            pick_vars[pkg] = (apk_v, cfg_v)
            row = ttk.Frame(listf); row.pack(anchor="w", fill="x")
            self._app_name_label(row, prof, pkg, label=labels.get(pkg))
            apk_cb = ttk.Checkbutton(row, text="APK", variable=apk_v)
            cfg_tip = f"Bundle {pkg}'s data/settings/BIOS (whole data dir for the launcher)"
            apk_tip = f"Bundle {pkg}'s installer (off = clean install / system launcher)"
            if is_launcher:
                apk_cb.configure(state="disabled")
                cfg_tip = f"Capture {pkg}'s state — its homescreen layout / emulator picks"
            elif pkg in apk_locked:
                apk_v.set(True); apk_cb.configure(state="disabled")   # always-install: APK locked ON
                apk_tip = f"{pkg} is always-installed (locked on) — manage it in Settings → Managed APKs."
            _tip(apk_cb, apk_tip).pack(side="left")
            cfg_cb = ttk.Checkbutton(row, text="Config", variable=cfg_v)
            if cfg_off:
                cfg_cb.configure(state="disabled")
                cfg_tip = f"No captured config for {pkg} — nothing to restore."
            _tip(cfg_cb, cfg_tip).pack(side="left")
        if flag_specs:
            ttk.Label(listf, text=flags_caption).pack(anchor="w", pady=(6, 0))
            for key, label, tip, init in flag_specs:
                fv = tk.BooleanVar(value=init)
                flag_vars[key] = fv
                _tip(ttk.Checkbutton(listf, text=f"{label}  (@{key})", variable=fv), tip).pack(anchor="w")

        win.wait_window()
        if "axes" not in result:
            return None
        return result["axes"], result["flags"]

    def _row_model(self, serial):
        """The model-column text for a device row (for nicer modal titles), or the serial as fallback."""
        try:
            vals = self.dev_tree.item(serial).get("values") or []
            return (str(vals[0]) if vals else "") or serial
        except tk.TclError:
            return serial

    def _pick_capture(self, serial, name):
        """Run-time SAVE picker: scan the connected device, show the modal, write the capture-manifest.
        Returns True to proceed with the capture, False if the operator cancelled."""
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
        device_apps = self._scan_device_apps(serial)
        gl, hl = self._detect_device_launchers(serial)
        sel = P.initial_capture_selection(device_apps, prof.capture_axes(), prof.capture_flags(),
                                          game_launcher=gl, home_launcher=hl,
                                          always_install=config.always_install_pkgs())
        # The launchers are NOT app rows — they're @gamelauncher / @homescreen behavior flags (below).
        # Pull their default Config bit out of the selection to seed those flags; leave only real apps.
        gl_on = sel.pop(gl, (False, True))[1] if gl else None
        hl_on = sel.pop(hl, (False, True))[1] if hl else None
        rows = sel
        cf = prof.capture_flags()
        # Full behaviour set so the operator SEES everything the golden carries. @homescreen is ALWAYS
        # shown — capture.sh resolves the HOME launcher itself on-device, so the operator controls it even
        # when GUI detection comes back empty. @gamelauncher is shown ONLY when a game frontend is detected
        # (no frontend → nothing to capture). @settings/@grants gate the device-settings / SAF-grant
        # capture; @hardening has nothing to capture (the golden's DEFAULT Download policy). All of them seed
        # the Download manifest (seed_default_manifest), so what's ticked here pre-fills Download; restore.sh
        # honours each @flag on the device.
        tips = {
            "settings": "Capture this device's display/brightness/animation/screen-timeout settings into the "
                        "golden (and apply them by default on Download).",
            "hardening": "The golden's DEFAULT on Download: keep emulators awake (battery-optimization "
                         "exempt) and block OTA updates that could break root. Applied at Download — set "
                         "the default here.",
            "grants": "Capture the SAF folder-access grants (so ES-DE/emulators read the ROM/BIOS dirs) "
                      "into the golden (and restore them by default on Download).",
            "homescreen": "Capture this device's homescreen layout (icon/folder/dock arrangement + "
                          "wallpaper) — and bundle the installers for the apps you placed so every "
                          "icon resolves on any unit model — into the golden (restored by default on Download).",
            "gamelauncher": "Capture the game frontend's per-system emulator picks (PSX→DuckStation, …) "
                            "into the golden (and apply them by default on Download).",
            "wifi": "Capture the golden's saved WiFi so fresh units auto-join it on Download (to pull "
                    "updates). ALWAYS stripped at Lock — no unit ever ships with the network/PSK.",
        }
        inits = {"settings": cf.get("settings", "on") == "on",
                 "hardening": cf.get("hardening", "on") == "on",
                 "grants": cf.get("grants", "on") == "on",
                 "wifi": cf.get("wifi", "on") == "on",
                 # ALWAYS shown: seed from a detected HOME launcher, else the saved/default flag.
                 "homescreen": hl_on if hl else (cf.get("homescreen", "on") == "on")}
        if gl:
            inits["gamelauncher"] = gl_on                  # shown ONLY when a game frontend is detected
        flag_specs = [(fl, _DL_FLAG_LABELS[fl], tips[fl], inits[fl]) for fl in _DL_FLAGS if fl in inits]
        res = self._app_pick_modal(
            f"Save — capture {self._row_model(serial)} into “{name}”",
            "Tick what to CAPTURE from this device into the golden. APK bundles the installer; Config "
            "bundles its data/settings/BIOS. The behaviour items below are saved with the golden and "
            "become its defaults on Download.",
            prof, rows, set(), flag_specs=flag_specs,
            flags_caption="— behavior (saved with the golden; default on Download) —",
            apk_locked=set(rows) & set(config.always_install_pkgs()))
        if res is None:
            self.log("Save cancelled — nothing captured.")
            return False
        axes, modal_flags = res
        flags = dict(cf); flags.update(modal_flags)        # all five @flags come straight from the modal
        pkgs, axes_sub = _manifest_from_axes(axes)         # real apps only (launchers are flags, not pkgs)
        P.save_manifest(prof.capture_manifest_path, pkgs, flags,
                        header=f"# {prof.name} capture", axes=axes_sub)
        self.log(f"capture selection for {prof.name}: {len(pkgs)} app(s) + flags={flags}")
        return True

    def _pick_downloads(self, serials):
        """Run-time DOWNLOAD picker: one modal per DISTINCT assigned profile among `serials`. Stashes each
        confirmed profile's selection and writes all manifests only AFTER every modal is confirmed, so a
        Cancel on any aborts the whole run with nothing written. Returns True to proceed, False on cancel."""
        names = []
        for s in serials:
            n = self.assigned.get(s)
            if n and n != "(no match)" and n not in names:
                names.append(n)
        pending = []                                       # (manifest_path, pkgs, flags, axes, name)
        for name in names:
            prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
            launcher_pkg = prof.launcher_pkg()
            # the launcher isn't an app row — it's a device SYSTEM app (e.g. com.android.launcher3, never
            # captured), and its homescreen rides the @homescreen behavior flag below.
            own_pkgs = [p for p in prof.all_pkgs() if p != launcher_pkg]
            store_pkgs = [a["pkg"] for a in P.list_store_apks(config.apk_store_dir())]
            # Golden-driven defaults: a captured app is pre-ticked (APK only if the golden bundled one —
            # config-only/sideloaded apps stay APK-off; Config only if it was captured); a store-only app
            # is NOT in the golden, so it's listed but un-ticked (opt in to push it).
            has_apk = {p: prof.has_captured_apk(p) for p in own_pkgs}
            has_cfg = {p: prof.has_captured_config(p) for p in own_pkgs}
            rows, cfg_disabled = P.download_rows(own_pkgs, store_pkgs, has_apk, has_cfg,
                                                 always_install=config.always_install_pkgs())
            labels = {}
            for p in store_pkgs:
                if p not in own_pkgs:
                    labels[p] = f"{p}  ·  from store"
            flags = prof.flags()
            flag_specs = [(fl, _DL_FLAG_LABELS[fl], _DL_FLAG_TIPS[fl], flags.get(fl, "on") == "on")
                          for fl in _DL_FLAGS]
            res = self._app_pick_modal(
                f"Download — restore “{name}”",
                "Tick which apps to INSTALL on the device(s) assigned this profile. APK installs the app; "
                "Config restores its saved data/settings/BIOS. Apps not captured in the golden (marked "
                "“from store”) are OFF by default — tick to push the server’s current build. Config is "
                "available only where the golden captured it. Behaviour @flags below apply on the device.",
                prof, rows, set(), flag_specs, labels=labels, cfg_disabled=cfg_disabled,
                apk_locked=set(rows) & set(config.always_install_pkgs()))
            if res is None:
                self.log("Download cancelled — nothing installed.")
                return False
            axes, fl = res
            pkgs, axes_sub = _manifest_from_axes(axes)
            pending.append((prof.manifest_path, pkgs, fl, axes_sub, name))
        for mpath, pkgs, fl, axes_sub, name in pending:
            P.save_manifest(mpath, pkgs, fl, header=f"# {name}", axes=axes_sub)
            self.log(f"download selection for {name}: {len(pkgs)} app(s), flags={fl}")
        return True

    def _on_chain_tick(self):
        """Save ⟂ Download/Warm up/Lock: when Save is on, disable+clear Download/Warm up/Lock; when any of
        those is on, disable+clear Save. Root stays available in both chains. Save ALSO requires exactly
        one selected device — _sync_save_selection_gate() re-applies that on top, so it always has the
        final say on the checkbox's enabled state."""
        save_on = self.chain_vars["save"].get()
        unit_on = any(self.chain_vars[k].get() for k in ("download", "warmup", "lock"))
        for k in ("download", "warmup", "lock"):
            self.chain_cbs[k].configure(state="disabled" if save_on else "normal")
            if save_on:
                self.chain_vars[k].set(False)
        self.chain_cbs["save"].configure(state="disabled" if unit_on else "normal")
        if unit_on:
            self.chain_vars["save"].set(False)
        self._sync_save_selection_gate()
        self._update_run_state()                     # ← ticking an action can arm ▶ Run

    def run_chain(self):
        steps, err = self._resolve_chain({k: v.get() for k, v in self.chain_vars.items()})
        if err:
            messagebox.showinfo("CAS", err)
            return
        if "save" in steps:
            # Save captures ONE golden device. `_selected_serial()` returns only the TOPMOST selected
            # row, so on a 3-device selection this used to silently save just that one — the footer said
            # "3 of 4 devices selected", ▶ Run touched exactly one, and the other two got nothing with no
            # warning. The footer's blast-radius promise must never disagree with what Run actually does,
            # so gate on the SELECTION COUNT (not "is there a topmost row") before it ever reaches a
            # single-serial call.
            if len(self.dev_tree.selection()) != 1:
                messagebox.showinfo(
                    "CAS", "Save captures ONE golden device. Select a single device (or untick Save).")
                return
            self._run_save(steps, self._selected_serial())
        else:
            t = self._action_targets()
            if not t:
                return
            cleared = self._preflight(steps, t)           # skip hard-blocked, confirm risky, then run survivors
            if not cleared:
                return
            if "download" in steps and not self._pick_downloads(cleared):  # modal(s): choose apps (or cancel)
                return
            self._warmup_opts = None
            if "warmup" in steps:                          # modal: choose the warm-up timing (or cancel)
                opts = self._pick_warmup()
                if opts is None:
                    return
                self._warmup_opts = opts
            self._run_chain(steps, cleared)

    def _pick_warmup(self):
        """Ask how long ③ Warm up runs (modal, pre-filled with the current values), persist the choice as
        the new default, and return (dwell_s, settle_s) — or None if the operator cancelled."""
        dlg = D.WarmupDialog(self.win, config.warmup_dwell_s(), config.warmup_settle_s())
        if dlg.result is None:
            return None
        dwell, settle = dlg.result
        config.set_warmup_dwell_s(dwell)                   # sticky: pre-fills next time ("update how long")
        config.set_warmup_settle_s(settle)
        self.log(f"warm up: {dwell:g}s per app, {settle:g}s settle.")
        return dwell, settle

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

    def _action_targets(self):
        """The serials an action runs on: the SELECTED rows. (There is no apply-to-all mode any more —
        Select all / Ctrl+A does that job, and the footer states the count before you press ▶ Run.)"""
        serials = list(self.dev_tree.selection())
        if not serials:
            messagebox.showinfo("CAS", "Select one or more device rows first "
                                       "(Ctrl-click to add, Shift-click for a range, Ctrl+A for all).")
            return None
        return serials

    _CHAIN_ORDER = ("root", "save", "download", "warmup", "lock")

    def _resolve_chain(self, ticked):
        """Turn the ticked action checkboxes into an ordered, validated step list.
        Returns (steps_in_fixed_order, error_or_None). Save is mutually exclusive with Download/Warm up/Lock."""
        on = [k for k in App._CHAIN_ORDER if ticked.get(k)]
        if not on:
            return [], "Tick at least one action to run."
        if "save" in on and ("download" in on or "warmup" in on or "lock" in on):
            return [], "Save (golden capture) can't run with Download/Warm up/Lock — they're opposite directions."
        return on, None

    def _profile_map(self, serials):
        """({serial: Profile or None}, force_serials) from each device's ASSIGNED profile. A hand-assigned
        device joins force_serials so Root/Lock may flash past a model mismatch (already confirmed at assign)."""
        pm, force = {}, set()
        for s in serials:
            name = self.assigned.get(s)
            if not name or name == "(no match)":
                pm[s] = None
                continue
            pm[s] = P.Profile(P.pathlib.Path(self.profiles_root) / name)
            if s in self.assigned_manual:
                force.add(s)
        return pm, force

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
        """Post a context menu with tk_popup()'s grab HELD until the menu actually goes away — that grab
        is what makes an outside click dismiss it (Tk delivers the click to the grabbing widget, whose own
        bindings then unpost it). ON X11 — the bench's platform — `tk_popup()` takes that grab and returns
        immediately, so releasing it in a `finally` right after (a widely-copied but WRONG recipe) drops it
        a microsecond after taking it, leaving the menu stuck open with nothing watching for the outside
        click. Windows/macOS post through the OS's own blocking menu loop and take no Tk grab at all, so
        they dismiss natively and the bindings below are inert there (grab_release() on an ungrabbed widget
        is a no-op). So instead: release exactly on <Unmap> (fires however the menu goes away —
        an item picked, Escape, an outside click, or the explicit unpost() below), plus belt-and-braces
        <FocusOut>/<Escape> dismissal. A stuck grab would freeze the WHOLE app on a bench, so <Unmap> is
        the one thing guaranteed to fire whenever the menu disappears — by any route, including the
        window being destroyed — which makes a stuck grab impossible."""
        def _release(_e=None):
            try:
                menu.grab_release()
            except tk.TclError:
                pass
        menu.bind("<Unmap>", _release)
        menu.bind("<FocusOut>", lambda _e: menu.unpost())
        menu.bind("<Escape>", lambda _e: menu.unpost())
        try:
            menu.tk_popup(event.x_root, event.y_root)
        except tk.TclError:
            _release()

    def _row_state(self, serial):
        vals = self.dev_tree.item(serial).get("values") or []
        return str(vals[4]).replace("● ", "") if len(vals) > 4 else ""

    def _rebuild_context_menu(self, serials):
        m = self.ctx
        m.delete(0, "end")
        # DESTROY the previous build's submenu widgets before dropping their refs — tkinter does NOT
        # destroy the underlying Tcl widget on Python GC, so every right-click leaked 3 orphan Menu
        # widgets (profile/firmware/run submenus) forever without this.
        for sub in self._ctx_subs:
            try:
                sub.destroy()
            except tk.TclError:
                pass
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
        m.add_command(label="Seal (retail lock)…", state=st("seal"),
                      command=lambda s=one: self.seal_selected(s))
        m.add_command(label="Release (un-provision)…", state=st("release"),
                      command=lambda s=one: self.release_selected(s))
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
        dk = config.default_kit_firmware()                   # what '(default kit)' maps to, if designated
        for fid in [FW.DEFAULT_FW_ID] + ids:                 # the default kit is a first-class choice
            text = fid
            if fid == FW.DEFAULT_FW_ID:
                text = f"{fid}  →  {dk}" if dk else f"{fid}  (set one in Firmware library…)"
            sub.add_command(label=("●  " if fid == shown else "○  ") + text,
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

    def _select_all_shortcut(self, event=None):
        """Ctrl+A / ⌘A → select every device, from anywhere on the window. Skips text widgets so their
        own Ctrl+A (select-all / start-of-line) still works — the read-only log, and any entry field.
        Returns 'break' so the tree's copy of this binding doesn't also fire the window-level one."""
        if isinstance(self.win.focus_get(), (tk.Entry, tk.Text)):
            return None
        self.select_all_devices()
        return "break"

    # ---------- firmware library (DEVICE ROOT firmware; library-only — never flashes) ----------
    def _fw_cell(self, serial):
        """The 'firmware' column text for a device row, from its resolved suggestion/override."""
        r = self.fw_resolved.get(serial)
        if r is None:
            return ""
        if not r.get("firmware_id"):
            return "(no match)"
        return r["firmware_id"] + ("" if r.get("ok") else " ⚠")

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

    def _open_apk_store(self):
        """Manage the server-side APK store (config.apk_store_dir()): list packages, Add/Update a build
        (sets it CURRENT — every config that lists the app then deploys it), or Remove (soft — clears
        current, keeps files). Shared across ALL profiles; uploads go to the library by default."""
        store = config.apk_store_dir()
        dlg = tk.Toplevel(self.win); dlg.title("Managed APKs (server store)"); dlg.transient(self.win)
        dlg.grab_set()
        tk.Label(dlg, text=f"Server store: {store}", anchor="w").pack(fill="x", padx=8, pady=(8, 4))
        # Pin the button row to the BOTTOM before the tree so it can never be clipped on a short window;
        # its buttons are added lower down (where their command closures are defined).
        bar = tk.Frame(dlg); bar.pack(fill="x", side="bottom", padx=8, pady=8)
        tree = ttk.Treeview(dlg, columns=("pkg", "label", "files", "always"), show="headings", height=12)
        for c, w in (("pkg", 320), ("label", 150), ("files", 55), ("always", 60)):
            tree.heading(c, text=c.upper()); tree.column(c, width=w)
        THEME.center_columns(tree)
        tree.pack(fill="both", expand=True, padx=8)
        tree.bind("<Double-1>", lambda e: toggle_always())   # double-click a row toggles Always-install

        def refresh():
            tree.delete(*tree.get_children())
            ai = set(config.always_install_pkgs() or ())     # marked ● in the ALWAYS column
            for a in P.list_store_apks(config.apk_store_dir()):
                mark = "●" if a["pkg"] in ai else ""
                tree.insert("", "end", iid=a["pkg"], values=(a["pkg"], a["label"], a["nfiles"], mark))

        def _sel():
            s = tree.selection()
            return s[0] if s else None

        def _put(pkg, preset_file=None):
            f = preset_file or filedialog.askopenfilename(
                title=f"Choose the APK for {pkg}", filetypes=[("APK", "*.apk"), ("All files", "*.*")])
            if not f:
                return
            got = P.apk_package_id(f)                 # guard: don't upload the wrong APK under this pkg
            if got and got != pkg and not messagebox.askyesno(
                    "CAS — package id mismatch",
                    f"This APK's package id is:\n    {got}\n\nbut you're updating:\n    {pkg}\n\n"
                    "Upload it anyway?"):
                return
            label = simpledialog.askstring("Version label", "Version label (blank = use the file name):",
                                           initialvalue=pathlib.Path(f).stem, parent=dlg) or None

            def work():
                lbl = P.put_store_apk(config.apk_store_dir(), pkg, f, label=label)
                self.log(f"server store: {pkg} → {lbl} (current).")
                self.win.after(0, refresh)
                return True
            self._run_bg(work, label=f"Uploading {pkg} to the store")

        def add():
            # Pick the APK first; read its package id straight from the file so the operator doesn't type
            # (and mistype) it. They can still edit it — e.g. if the manifest couldn't be parsed.
            f = filedialog.askopenfilename(title="Choose the APK to add (its package id is read from the file)",
                                           filetypes=[("APK", "*.apk"), ("All files", "*.*")])
            if not f:
                return
            detected = P.apk_package_id(f)
            pkg = simpledialog.askstring(
                "Add APK — package id",
                "Package id read from the APK (edit if wrong):" if detected
                else "Couldn't read the package id from this file — enter it (e.g. org.cocoon.app):",
                initialvalue=detected or "", parent=dlg)
            if pkg and pkg.strip():
                _put(pkg.strip(), preset_file=f)

        def update():
            pkg = _sel()
            if not pkg:
                messagebox.showinfo("CAS", "Select a package row to update.")
                return
            _put(pkg)

        def remove():
            pkg = _sel()
            if not pkg:
                messagebox.showinfo("CAS", "Select a package row to remove.")
                return
            if messagebox.askyesno("CAS", f"Stop deploying {pkg}?\nFiles stay on the server (soft-remove)."):
                P.remove_store_apk(config.apk_store_dir(), pkg)
                self.log(f"server store: {pkg} removed (soft — files retained).")
                refresh()

        def toggle_always():
            # Flip the selected package's membership in the GLOBAL always-install set (● in the ALWAYS
            # column). Always-install apps have their APK forced-on by default in the Save/Download pickers.
            pkg = _sel()
            if not pkg:
                messagebox.showinfo("CAS", "Select a package row to toggle Always-install.")
                return
            new = P.toggle_always_member(config.always_install_pkgs(), pkg)
            config.set_always_install_pkgs(sorted(new))
            self.log(f"always-install: {pkg} {'ON' if pkg in new else 'off'} "
                     f"({len(new)} app(s) always-installed).")
            refresh()

        def install_to_devices():
            # Ad-hoc: push the selected store app to the currently SELECTED device row(s) in the main
            # window (same target resolution as ▶ Run — see _action_targets). Plain user install — no
            # profile/golden/root. Off-thread so the UI never freezes.
            pkg = _sel()
            if not pkg:
                messagebox.showinfo("CAS", "Select a package row to install to the device(s).")
                return
            serials = self._action_targets()
            if not serials:
                return
            if not messagebox.askyesno(
                    "CAS — install to device(s)",
                    f"Install {pkg} (current store build) to {len(serials)} device(s)?\n\n" + "\n".join(serials)):
                return

            def work():
                res = PV.install_store_app_pc(config.apk_store_dir(), pkg,
                                              lambda s: Adb(serial=s, adb=self.adb_bin), serials, self.log)
                ok = sum(1 for v in res.values() if v)
                self.log(f"ad-hoc install: {pkg} → {ok}/{len(serials)} device(s) OK.")
                return True
            self._run_bg(work, label=f"Installing {pkg} → {len(serials)} device(s)")

        # `bar` was created + bottom-pinned above (before the tree) so it can't be clipped; fill it now.
        # ttk.Button (not tk.Button) so these inherit the themed thin-black-border look like everywhere else.
        for txt, cmd in (("Add APK…", add), ("Update…", update), ("Toggle Always", toggle_always),
                         ("Install → device(s)", install_to_devices), ("Remove", remove),
                         ("Close", dlg.destroy)):
            ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=4)
        refresh()
        D.size_to_content(dlg, self.win, 720, 420)   # open tall enough + on-screen; buttons never clipped

    def _open_profiles(self):
        D.ProfilesWindow(self.win, self)

    def _open_firmware(self):
        D.FirmwareWindow(self.win, self)

    def _open_boxart(self):
        D.BoxArtDialog(self.win, self)

    def _open_history(self):
        D.HistoryWindow(self.win, self)

    def _add_firmware(self, on_done=None):
        """Ingest a raw firmware build folder into the library (new version) on a background thread.
        Prompts for the firmware id (variants that share a model — e.g. MQ65 vs MQ66, both 'AIR X' —
        need DISTINCT ids) and an optional serial prefix that drives the per-device auto-match.

        `on_done`, if given, is invoked on the UI thread once the ingest actually finishes — the copy
        can be multi-GB over USB/NAS, so callers must never guess a fixed delay (a caller-owned window
        may have to refresh its own list once the real data has landed)."""
        # Check BEFORE prompting: _run_bg refuses (logs 'busy') while a job runs, so without this the
        # operator would answer the folder/id/prefix dialogs only for the ingest — and on_done — to never
        # fire, leaving the Firmware window silently stale.
        if self.busy:
            messagebox.showinfo("CAS", "Finish the current operation first, then add firmware.")
            return
        folder = filedialog.askdirectory(title="Select a firmware build folder (contains emmc/ or ufs/)")
        if not folder:
            return
        fid = simpledialog.askstring(
            "Firmware id",
            "Firmware id — variants that share a model need DISTINCT ids:\n"
            "  e.g. mangmi-air-x-mq66   (the MQ65 build gets mangmi-air-x-mq65)\n"
            "Re-using an id adds a new VERSION to that firmware instead.",
            parent=self.win)
        if not fid or not fid.strip():
            return
        prefix = simpledialog.askstring(
            "Serial prefix (auto-match)",
            "Device serial prefix(es) for auto-match, comma-separated — e.g. MQ66\n"
            "(MQ65/MQ66 both report model 'AIR X', so the serial prefix is what tells them apart).\n"
            "Leave blank to match by model only.",
            parent=self.win) or ""
        prefixes = [p.strip() for p in prefix.split(",") if p.strip()]
        match = {"serial_prefix": prefixes} if prefixes else None

        def work():
            fw = FW.ingest(folder, FW.firmware_root(), firmware_id=fid.strip(), match=match)
            self.log(f"firmware ingested: {fw.id}  v{fw.current()}  match={fw.match_rules()}")
            self.win.after(0, self.refresh_devices)
            if on_done is not None:
                self.win.after(0, on_done)
            return True
        self._run_bg(work, label="Ingesting firmware")

    def _stage(self, step, serials, pm, force, cev, wait_boot=False):
        """Run ONE unit stage across serials via the matching PV.*_all; return its {serial:(status,…)} dict.
        wait_boot (Download only): block on each unit's post-Download reboot so a following Lock stage never
        starts on a rebooting device."""
        devs = [(s, "device") for s in serials]
        mk_adb = lambda s: Adb(serial=s, adb=self.adb_bin, cancel=cev)
        mk_fb = lambda s: Fastboot(serial=s, fastboot=self.fb_bin, cancel=cev)
        if step == "download":
            return PV.provision_all(mk_adb, devs, root=self.profiles_root, log=self.log,
                                    profile_map=pm, es_media_src=config.es_media_src(), wait_boot=wait_boot)
        if step == "root":
            return PV.root_all(mk_adb, mk_fb, devs, profiles_root=self.profiles_root, appdir=APPDIR,
                               log=self.log, profile_map=pm, force_serials=force,
                               on_critical=self._on_flash_critical)
        if step == "lock":
            return PV.seal_all(mk_adb, mk_fb, devs, profiles_root=self.profiles_root, appdir=APPDIR,
                               log=self.log, profile_map=pm, force_serials=force,
                               on_critical=self._on_flash_critical)
        if step == "warmup":
            dwell, settle = getattr(self, "_warmup_opts", None) or (None, None)  # from the ③ modal; None → config
            return PV.warmup_all(mk_adb, devs, root=self.profiles_root, log=self.log, profile_map=pm,
                                 parallel=True, dwell=dwell, settle=settle)
        raise ValueError(f"unknown step {step!r}")

    def _run_chain_core(self, steps, serials, save_name):
        """Pure chain loop (no Tk/threads): fold survivors across stages, return the final survivor list."""
        cev = self.cancel_event
        pm, force = self._profile_map(serials)
        survivors = list(serials)
        for i, step in enumerate(steps):
            if cev.is_set():
                break
            if step == "save":
                if not survivors:
                    break
                s = survivors[0]
                msgs = []                                  # capture the reason so a FAILED save is logged with it
                def _slog(m):
                    msgs.append(m)
                    self.log(m)
                ok = PV.capture_to_pc(Adb(serial=s, adb=self.adb_bin, cancel=cev), save_name, _stamp(),
                                      root=self.profiles_root, log=_slog)
                if not ok and not cev.is_set():            # success is logged by capture_to_pc; log the failure
                    PV.log_save_fail(self.profiles_root, save_name, s,
                                     msgs[-1] if msgs else "capture failed", self.log)
                survivors = survivors if ok else []
            else:
                # Download reboots WITHOUT waiting; if any step follows (Lock), make the Download stage block
                # on each unit's reboot so the next stage never touches an offline/rebooting device. Root and
                # Lock already wait for their own reboots internally.
                wb = step == "download" and bool(steps[i + 1:])
                res = self._stage(step, survivors, pm, force, cev, wait_boot=wb)
                survivors = [s for s in survivors if res.get(s, ("error",))[0] not in ("fail", "error")]
            self.log(f"chain: after {step} — {len(survivors)}/{len(serials)} still ok")
        return survivors

    def _run_chain(self, steps, serials, save_name=None):
        """Run the resolved chain on serials (one confirm, then background, per-stage survivor folding).
        NOTE: there is deliberately no "save needs exactly one serial" guard here — every caller already
        guarantees that (run_chain()'s explicit selection-count check before _run_save(), and _run_save()
        itself only ever passing a single-element `cleared` list). A guard here could never fire, and dead
        safety code that LOOKS like protection but never runs is worse than no code at all — see BLOCKING
        FINDING 1."""
        names = {"root": "Root", "save": "Save", "download": "Download", "warmup": "Warm up", "lock": "Lock"}
        chain = " → ".join(names[s] for s in steps)
        if not messagebox.askyesno("CAS — Run", f"Run {chain} on {len(serials)} device(s)?\nThey run IN PARALLEL per stage."):
            return
        def work():
            survivors = self._run_chain_core(steps, serials, save_name)
            self.win.after(0, self.refresh_devices)
            self.win.after(0, self.refresh_profiles)
            return self._chain_result(serials, survivors)
        self._run_bg(work, label=f"Running {chain} on {len(serials)} device(s)")

    @staticmethod
    def _chain_result(serials, survivors):
        """Build the {serial: (status, detail)} dict _report (and the retry detector) expect after a
        chain run: survivors are 'ok', everyone else 'fail'. MUST be (status, detail) 2-tuples with the
        'ok' success token — a bare/short tuple or a 'done' token makes _report crash or mislabel."""
        sset = set(survivors)
        return {s: ("ok", "") if s in sset else ("fail", "") for s in serials}

    def seal_selected(self, serial=None):
        """Operator-only: retail-SEAL the one selected unit on demand (the single-device slice of ④ Lock),
        paired with 'Release selected unit'. Runs the full seal via PV.seal_all([one device]) so firmware /
        EDL flasher / model-match brick-guard / golden-guard all behave exactly as the batch Lock.

        `serial`, when given, PINS the action to that device (the context menu passes it — snapshotted at
        BUILD time, like every other item there). Left None (the Settings-menu callers), it falls back to
        _selected_serial() — reading the CURRENT selection at click time — matching the old behaviour.
        Without the pin, a background refresh between right-click and click could change the selection
        underneath a bare `self.seal_selected` and fire this destructive action on the wrong device."""
        serial = serial if serial is not None else self._selected_serial()
        if not serial:
            messagebox.showinfo("CAS", "Select ONE device in the list first.")
            return
        if not messagebox.askyesno(
                "CAS — seal (retail-lock) unit?",
                f"Retail-seal {serial}?\n\n"
                "This un-roots the unit (flashes stock init_boot, ~2-3 min), hides Developer options, "
                "and disables USB debugging — adb WILL disconnect. The golden is skipped.\n\n"
                "Use for a one-off / re-seal outside the ④ Lock batch. Assumes the unit is VERIFIED."):
            return
        pm, force = self._profile_map([serial])
        def work():
            cev = self.cancel_event
            res = PV.seal_all(
                lambda s: Adb(serial=s, adb=self.adb_bin, cancel=cev),
                lambda s: Fastboot(serial=s, fastboot=self.fb_bin, cancel=cev),
                [(serial, "device")],
                profiles_root=self.profiles_root, appdir=APPDIR, log=self.log,
                profile_map=pm, force_serials=force, on_critical=self._on_flash_critical)
            self.win.after(0, self.refresh_devices)
            return res
        self._run_bg(work, label=f"Sealing {serial}")

    def release_selected(self, serial=None):
        """Operator-only: un-provision the selected unit (clear the Companion's Device-Owner lockdown so it
        can be factory-reset / uninstalled). Exceptional RMA action — single device, behind a confirm.

        `serial`, when given, PINS the action to that device (the context menu passes it — snapshotted at
        BUILD time). Left None (the Settings-menu callers), it falls back to _selected_serial(), matching
        the old behaviour. See seal_selected's docstring for why the pin matters."""
        serial = serial if serial is not None else self._selected_serial()
        if not serial:
            messagebox.showinfo("CAS", "Select ONE device in the list first.")
            return
        if not messagebox.askyesno(
                "CAS — release (un-provision) unit?",
                f"Clear the GameCove Companion lockdown on {serial}?\n\n"
                "After this, the app can be uninstalled and the unit can be factory-reset. "
                "Use this for RMA / repair / resale."):
            return
        def work():
            return PV.release(Adb(serial=serial, adb=self.adb_bin, cancel=self.cancel_event), log=self.log)
        self._run_bg(work, label=f"Releasing {serial}")

    def _lib_reachable(self):
        """Is the CURRENTLY-SELECTED library path (self.profiles_root) a reachable directory?
        (Checks the cached path the UI actually lists — consistent with refresh_profiles.)"""
        try:
            return P.pathlib.Path(self.profiles_root).is_dir()
        except OSError:
            return False

    def _update_lib_label(self):
        self.lib_var.set(_profile_library_label(self.profiles_root, self._lib_reachable()))

    def _lib_watch(self):
        """Idle poll (every 2s): when the library drive (re)appears, self-heal the UI so
        the operator need not click Refresh. On the unreachable→reachable edge (while idle)
        re-resolve profiles, firmware and devices; if a job is running, defer to the next
        tick. On removal, relabel honestly WITHOUT calling refresh_profiles, so a transient
        USB drop does not wipe the operator's Profile selection. Reschedules itself; stops
        quietly once the window is destroyed."""
        try:
            now = self._lib_reachable()
            action = _lib_watch_action(self._lib_last_reachable, now, self.busy)
            if action == "reconnect":
                self._lib_last_reachable = True
                self.log("library drive detected — refreshed")
                self.refresh_profiles()
                self.refresh_devices()
            elif action == "disconnect":
                self._lib_last_reachable = False
                self.log("library drive removed")
                self._update_lib_label()
            # action in (None, "defer") → leave the baseline untouched
        except tk.TclError:
            return                            # window gone — stop rescheduling
        self.win.after(2000, self._lib_watch)

    def _on_media_mode(self):
        """Inline ES-DE box-art radio changed (use the SD card vs push from a PC folder) — persist + react.
        SD mode auto-detects the card's box art; push mode skips detection (we're uploading anyway)."""
        if self.media_mode.get() == "push":
            p = self.media_path.get().strip()
            if p:
                config.set_es_media_src(p)
                self.log(f"ES-DE box art: will PUSH from {p} to each unit on Download.")
            # empty path -> leave unset; the operator still needs to Browse to a folder
            self.sd_media_var.set("(push mode — box art comes from the PC folder; SD not checked)")
        else:
            config.set_es_media_src(None)
            self.log("ES-DE box art: using each device's SD card (no per-unit push).")
            self._probe_sd_media()                          # SD mode -> verify the card actually has box art

    def _browse_media(self):
        """Pick a PC folder to push box art from (selects the 'push' mode)."""
        d = filedialog.askdirectory(title="ES-DE box-art folder (an 'ES-DE' or 'downloaded_media' folder)")
        if d:
            self.media_path.set(d)
            self.media_mode.set("push")
            config.set_es_media_src(d)
            self.log(f"ES-DE box art: will PUSH from {d} to each unit on Download.")

    def _probe_sd_media(self, serial=None):
        """AUTO-DETECT whether a device's SD carries an ES-DE folder / box art, shown inline (no button).
        Probes the selected device, or the only connected one. Best-effort, threaded — never blocks."""
        if not hasattr(self, "sd_media_var"):
            return
        if getattr(self, "media_mode", None) is not None and self.media_mode.get() != "sd":
            self.sd_media_var.set("(push mode — box art comes from the PC folder; SD not checked)")
            return                                          # push/upload mode -> no SD detection needed
        serial = serial or self._selected_serial()
        if not serial:
            kids = self.dev_tree.get_children()
            serial = kids[0] if len(kids) == 1 else None
        if not serial:
            self.sd_media_var.set("SD: select a device to auto-detect ES-DE / box art")
            return
        self.sd_media_var.set("SD: detecting…")

        def work():
            try:
                a = Adb(serial=serial, adb=self.adb_bin)
                # match ANY external volume-id format (a big exFAT card mounts hyphen-LESS), not just /*-*/
                out = a.shell("ls -d /storage/*/ES-DE /storage/*/ES-DE/downloaded_media "
                              "/storage/*/downloaded_media 2>/dev/null")[1].strip()
                paths = out.splitlines()
                esde = any(p.rstrip("/").endswith("/ES-DE") for p in paths)
                art = any(p.rstrip("/").endswith("downloaded_media") for p in paths)
                if esde and art:
                    msg = "SD: ✓ ES-DE folder + box art on the card"
                elif esde:
                    msg = "SD: ✓ ES-DE folder on the card — no box art (downloaded_media) inside"
                elif art:
                    msg = "SD: ✓ box art on the card — no ES-DE folder"
                else:
                    msg = "SD: ✗ no ES-DE folder or box art on the card"
            except Exception as e:
                msg = f"SD: could not read ({e})"
            self.win.after(0, lambda m=msg: self.sd_media_var.set(m))
        threading.Thread(target=work, daemon=True).start()

    def _scroll_tab(self, parent):
        """A vertically-scrollable frame filling `parent` above any bottom-pinned controls; returns the
        inner frame to pack rows into. The canvas tracks its width onto the inner frame and the wheel
        scrolls while the pointer is over it. Used by the app-pick modal's list area."""
        wrap = ttk.Frame(parent)
        wrap.pack(side="top", fill="both", expand=True)
        cv = tk.Canvas(wrap, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(cv)
        mid = cv.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfigure(mid, width=e.width))

        def _wheel(e):                                    # cross-platform wheel: Win/Mac delta, X11 Button-4/5
            step = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
            cv.yview_scroll(step, "units")
        def _unbind():                                    # drop the global wheel hooks (Leave, or on destroy)
            cv.unbind_all("<MouseWheel>"); cv.unbind_all("<Button-4>"); cv.unbind_all("<Button-5>")
        cv.bind("<Enter>", lambda e: (cv.bind_all("<MouseWheel>", _wheel),
                                      cv.bind_all("<Button-4>", _wheel), cv.bind_all("<Button-5>", _wheel)))
        cv.bind("<Leave>", lambda e: _unbind())
        cv.bind("<Destroy>", lambda e: _unbind())         # modal closed with pointer inside → no dangling bind
        return inner

    def _app_name_label(self, row, prof, pkg, label=None):
        """Left-aligned app cell carrying the app's launcher icon (real APK icon → curated logo → coloured
        placeholder) + the friendly name, plus a dim 'mini' line with the raw package id when the name
        doesn't already reveal it — so every Save/Download row shows EXACTLY which com.xxx/xyz.xxx it is
        (e.g. AetherSX2 = .android vs NetherSX2 = .tturnip). `label` overrides the friendly name — used to
        give the launcher rows a role label (e.g. 'Home launcher · homescreen') or store annotations."""
        text = label or _app_label(pkg)
        icon = self._app_icon(prof, pkg) or self._placeholder_icon(text)
        cell = ttk.Frame(row)
        cell.pack(side="left")
        name = ttk.Label(cell, text=f" {text}", width=28)    # width=28 keeps the APK/Config columns aligned
        if icon is not None:
            name.configure(image=icon, compound="left")
        name.pack(side="top", anchor="w")
        if pkg not in text:                                  # skip a redundant id line for store/unmapped rows
            ttk.Label(cell, text=f"      {pkg}", foreground="#8a8a8a", font=("", 8)).pack(side="top", anchor="w")
        return cell

    ICON_PX = 24                                          # uniform icon box for the app list

    def _make_icon(self, data):
        """A uniform-size icon (a PhotoImage) from raw image bytes, or None. Pillow first — it decodes
        PNG *and WEBP* (most modern app icons) and resizes cleanly; falls back to Tk's PNG-only PhotoImage
        so it still works without Pillow. NEVER raises."""
        t = self.ICON_PX
        try:
            import io
            from PIL import Image, ImageTk
            im = Image.open(io.BytesIO(data)).convert("RGBA")
            im.thumbnail((t, t))
            img = ImageTk.PhotoImage(im)
            self.__dict__.setdefault("_icon_refs", []).append(img)
            return img
        except Exception:
            pass
        try:
            import base64
            img = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))   # PNG only
            w = img.width() or 0
            if w > t + 6:
                img = img.subsample(max(1, w // t))
            self.__dict__.setdefault("_icon_refs", []).append(img)
            return img
        except Exception:
            return None

    def _icon_from_apks(self, apks):
        """Best app icon (PNG or WEBP) from the given APK(s) as a uniform PhotoImage, or None. APKs are
        zips; apps don't all name their icon 'ic_launcher', so we score candidates by likelihood and pick
        the lowest tier, then the biggest (highest-res):
          tier 0 — name has launcher/foreground/app_icon  (the real launcher icon)
          tier 1 — name has icon/logo                     (custom-named icons, e.g. RetroArch)
          tier 2 — ANY raster under res/mipmap-*          (that dir is launcher-icons by convention)
        Skips round/background/notification/banner/splash/monochrome layers. PNG works without Pillow;
        WEBP needs Pillow. NEVER raises."""
        try:
            import zipfile
            best = None                                  # (tier, size, bytes): lower tier wins, then bigger
            for apk in apks:
                apk = pathlib.Path(apk)
                if not apk.is_file():
                    continue
                with zipfile.ZipFile(apk) as z:
                    for n in z.namelist():
                        ln = n.lower()
                        if not (ln.endswith(".png") or ln.endswith(".webp")):
                            continue
                        if not (ln.startswith("res/") and ("/mipmap" in ln or "/drawable" in ln)):
                            continue
                        if any(b in ln for b in ("round", "background", "notification",
                                                 "banner", "splash", "feature", "monochrome")):
                            continue
                        base = ln.rsplit("/", 1)[-1]
                        if any(k in base for k in ("ic_launcher", "launcher", "app_icon", "appicon",
                                                   "foreground")):
                            tier = 0
                        elif "icon" in base or "logo" in base:
                            tier = 1
                        elif "/mipmap" in ln:
                            tier = 2
                        else:
                            continue                     # a generic drawable — likely a UI asset, skip
                        size = z.getinfo(n).file_size
                        if best is None or tier < best[0] or (tier == best[0] and size > best[1]):
                            best = (tier, size, z.read(n))
            return self._make_icon(best[2]) if best else None
        except Exception:
            return None

    def _app_icon(self, prof, pkg):
        """Icon for a package, in priority order:
          1. a CURATED logo bundled with CAS at assets/app-icons/<pkg>.(png|webp) — GUARANTEES a clean icon
             for apps whose APK ships only a vector adaptive icon (nothing a pure-Python reader can raster),
             e.g. some of RetroArch/ES-DE/Dolphin builds. Drop a PNG there to fix any app for good.
          2. else the app's OWN launcher icon, extracted from its bundled APK (covers most apps).
        Returns a uniform PhotoImage or None (caller then shows a colored chip)."""
        for ext in (".png", ".webp"):
            f = BUNDLE / "assets" / "app-icons" / (pkg + ext)
            try:
                if f.is_file():
                    img = self._make_icon(f.read_bytes())
                    if img is not None:
                        return img
            except OSError:
                pass
        apkdir = pathlib.Path(prof.payload) / pkg / "apk"
        apks = sorted(apkdir.glob("*.apk")) if apkdir.is_dir() else []
        return self._icon_from_apks(apks) if apks else None

    # a small, readable palette — each app gets a STABLE colour from its name (avatar-chip style)
    _PH_PALETTE = ["#A855F7", "#6366F1", "#0EA5E9", "#10B981", "#F59E0B", "#EF4444", "#EC4899", "#14B8A6"]

    def _placeholder_icon(self, label):
        """A uniform fallback tile for apps whose real icon can't be decoded — so EVERY row gets an equal
        slot instead of a jagged mix of icons and blanks. Colour is stable per app (hashed from the name).
        With Pillow: a rounded coloured tile with the name's initial. Without Pillow: a solid coloured chip
        (Tk PhotoImage.put). Cached per label; refs kept so Tk won't GC them."""
        key = (label or "?").strip() or "?"
        cache = self.__dict__.setdefault("_ph_icons", {})
        if key in cache:
            return cache[key]
        import hashlib
        hexc = self._PH_PALETTE[int(hashlib.md5(key.encode()).hexdigest(), 16) % len(self._PH_PALETTE)]
        text = key[:1].upper()
        t = self.ICON_PX
        img = None
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
            rgb = tuple(int(hexc[i:i + 2], 16) for i in (1, 3, 5))
            im = Image.new("RGBA", (t, t), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.rounded_rectangle([0, 0, t - 1, t - 1], radius=6, fill=rgb + (255,))
            font = ImageFont.load_default()
            bb = d.textbbox((0, 0), text, font=font)
            d.text(((t - (bb[2] - bb[0])) / 2 - bb[0], (t - (bb[3] - bb[1])) / 2 - bb[1]),
                   text, fill="white", font=font)
            img = ImageTk.PhotoImage(im)
        except Exception:
            try:
                img = tk.PhotoImage(width=t, height=t)
                img.put(hexc, to=(0, 0, t, t))            # solid colour chip (no Pillow needed)
            except Exception:
                img = None
        cache[key] = img
        return img

    def new_profile(self):
        """Create an empty profile and RETURN its name (the ProfilePicker selects the result), or None
        if the operator cancelled / the name was taken."""
        # No regex needed — the profile auto-matches by NAME similarity + SD size. Put the device model and
        # the SD capacity in the name (e.g. 'retroid-pocket-6-512', 'retroid-pocket-6-256') and CAS matches
        # a Retroid Pocket 6 with a ~512 GB card to the -512 profile, a ~256 GB card to the -256 profile.
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
        # model_match left blank on purpose — name-similarity handles it. (Set it by hand only for odd model
        # strings the name can't capture; an explicit model_match still takes precedence.)
        (d / "profile.meta").write_text("model_match=\nfrontend=\nnotes=\ncaptured=\n")
        (d / "manifest").write_text(f"# {name} (empty — capture a golden to populate)\n")
        self.log(f"created profile '{name}' — auto-matches by name + SD size, no regex needed. "
                 "Select the golden device, then 'Save device → profile'.")
        self.refresh_profiles()
        return name

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
        # forget any remembered device assignments pointing at the now-deleted profile (in memory + on disk)
        for s in [s for s, n in list(self.assigned.items()) if n == name]:
            self.assigned.pop(s, None)
            self.assigned_manual.discard(s)
            config.set_device_profile(s, None)
        self.log(f"archived '{name}' -> {dst}")
        self.refresh_profiles()
        self.refresh_devices()

    def set_profile_model(self, name):
        """Edit a profile's model-match — the device model this profile auto-assigns to. Prompts with the
        current value, writes it to profile.meta (other keys preserved), and re-resolves device matches.
        Returns the new value on save, or None if cancelled."""
        if not name:
            return None
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
        cur = prof.meta.get("model_match", "") or ""
        new = simpledialog.askstring(
            "Profile model",
            f"Model match for “{name}” — the device model this profile auto-assigns to.\n"
            "A substring or regex matched against the device's model, e.g. 'Odin2.*Mini' or "
            "'Retroid Pocket 6'. Leave blank to match by profile name + SD size only.",
            initialvalue=cur, parent=self.win)
        if new is None:                                    # cancelled — leave it untouched
            return None
        new = new.strip()
        P.set_meta_key(prof.path / "profile.meta", "model_match", new)
        self.log(f"profile '{name}' model → '{new or '(name + SD-size match)'}'")
        self.refresh_devices()                             # re-resolve auto-matches with the new pattern
        return new


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


if __name__ == "__main__":
    main()
