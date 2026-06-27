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
from .adb import Adb, Fastboot, list_devices
from . import config
from .config import library_root


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


class _Tooltip:
    """Minimal hover tooltip (no external deps): a yellow popup on <Enter>, gone on <Leave>."""
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
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)            # no title bar
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, wraplength=380, padx=7, pady=5).pack()

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
    "xyz.aethersx2.tturnip": "AetherSX2  ·  PS2",
    "me.magnum.melonds.nightly": "melonDS  ·  DS",
    "org.citra.emu": "Citra  ·  3DS",
    "org.ppsspp.ppsspp": "PPSSPP  ·  PSP",
    "org.mupen64plusae.v3.fzurita": "Mupen64Plus  ·  N64",
    "org.es_de.frontend": "ES-DE  ·  front-end",
    "gamehub.lite": "GameHub  ·  PC games",
    "com.gamecove.gamecove_companion": "GameCove Companion  ·  app",
}


def _app_label(pkg):
    """Human-friendly name for a package (falls back to the package id for anything unmapped)."""
    return _APP_LABELS.get(pkg, pkg)


def _human_size(nbytes):
    """Bytes -> '3.4 GB' / '512 MB' / '— ' for 0."""
    n = float(nbytes or 0)
    if n <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _human_eta(secs):
    """Seconds -> '45s' / '4m 05s' / '1h 12m'."""
    secs = int(max(0, secs or 0))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


class App:
    def __init__(self, win, adb_bin="adb", fb_bin="fastboot"):
        self.win = win
        self.adb_bin = adb_bin
        self.fb_bin = fb_bin
        self._nas_autoconnect()                 # log into the NAS (if app creds saved) before resolving
        self.profiles_root = str(library_root())
        self.logq = queue.Queue()
        self.busy = False
        self._tick_id = None        # after() id for the elapsed-time ticker
        self._action = ""           # label of the in-flight action
        self._t0 = 0.0
        self._last_line = ""        # most recent log line — shown live in the status bar
        self._retry_ctx = None      # (message, retry_callable) armed by an op that had failures
        self.pkg_vars = {}          # pkg -> tk.BooleanVar (manifest checkboxes)
        self.flag_vars = {}         # @flag -> tk.BooleanVar (settings/hardening/grants)
        self.assigned = {}          # serial -> profile name (per-device; remembered across launches)
        self.assigned_manual = set()  # serials whose profile was set by hand (deliberate; allows force)
        self._last_auto = {}        # serial -> last auto-match shown (to detect + announce auto-changes)
        self.fw_resolved = {}       # serial -> firmware.resolve() result dict (for the column + status line)
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

    # ---------- menu bar ----------
    def _build_menu(self):
        bar = tk.Menu(self.win)

        filem = tk.Menu(bar, tearoff=0)
        filem.add_command(label="Refresh devices", command=self.refresh_devices, accelerator="Ctrl+R")
        filem.add_separator()
        filem.add_command(label="Quit", command=self.win.destroy, accelerator="Ctrl+Q")
        bar.add_cascade(label="File", menu=filem)

        setm = tk.Menu(bar, tearoff=0)
        setm.add_command(label="Open library folder", command=self._open_library)
        setm.add_command(label="Log folder…", command=self.choose_log_dir)
        setm.add_separator()
        setm.add_command(label="NAS login…", command=self.nas_login_dialog)
        bar.add_cascade(label="Settings", menu=setm)

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
        """Open the storage location in the file manager. Opens the active library, but when the library
        has fallen back to local (NAS not mounted here) it opens the NAS share itself so you still land on
        192.168.100.227 — converting the Windows UNC path to an smb:// URL on Linux/macOS so the file
        manager mounts it on the fly."""
        from .config import NAS_DEFAULT, load_config
        target = str(self.profiles_root)
        explicit = os.environ.get("CAS_PROFILES") or load_config().get("library")
        if not explicit and target == str(APPDIR / "data" / "profiles"):
            target = NAS_DEFAULT          # default fell back to local — take the user to the NAS instead
        if sys.platform != "win32" and target.startswith("\\\\"):
            target = "smb://" + target[2:].replace("\\", "/")   # \\host\share\.. -> smb://host/share/..
        if not self._open_path(target):
            messagebox.showwarning(
                "CAS",
                f"Couldn't open a file manager for:\n{target}\n\n"
                "On Windows this opens in Explorer. On this machine, open it manually in your file "
                "manager (paste the address above).")

    def choose_log_dir(self):
        """Pick a shared folder (e.g. the mounted NAS) where the download/save run-history .jsonl logs are
        written, so they centralize across benches WITHOUT moving the heavy goldens off the local library.
        Cancel offers to clear it (logs then fall back to the library root). Logs always fall back to local
        if the chosen folder is later unreachable, so a run is never lost."""
        cur = config.load_config().get("log_dir")
        d = filedialog.askdirectory(
            title="Run-history log folder — shared/NAS folder for download/save logs  (Cancel to clear)")
        if d:
            config.set_log_dir(d)
            self.log(f"Run-history logs → {d}  (download-history.jsonl / save-history.jsonl).")
        elif cur and messagebox.askyesno(
                "CAS", "Clear the shared log folder? Run-history will go to the library root instead."):
            config.set_log_dir(None)
            self.log("Run-history logs → library root (shared log folder cleared).")

    def _open_path(self, target):
        """Open a folder path or smb:// URL in the OS file manager. Returns True if a viewer was launched.
        xdg-open often has no smb:// handler, so network URLs are routed straight to a file manager that
        speaks smb (KDE Dolphin, GNOME Files, etc.)."""
        try:
            if sys.platform == "win32":
                os.startfile(target)                     # noqa: Explorer handles UNC + local paths
                return True
            if sys.platform == "darwin":
                subprocess.Popen(["open", target])
                return True
            is_url = "://" in target
            order = (["dolphin", "nautilus", "nemo", "caja", "pcmanfm-qt", "pcmanfm", "thunar"]
                     if is_url else
                     ["xdg-open", "dolphin", "nautilus", "nemo", "caja", "pcmanfm-qt", "pcmanfm", "thunar"])
            for fm in order:
                if shutil.which(fm):
                    subprocess.Popen([fm, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
            return False
        except Exception as e:
            messagebox.showerror("CAS", f"Could not open:\n{target}\n\n{e}")
            return False

    def _about(self):
        from .config import NAS_DEFAULT, load_config
        p = str(self.profiles_root)
        if os.environ.get("CAS_PROFILES") or load_config().get("library"):
            where = "configured override"
        elif p == NAS_DEFAULT or p.startswith("\\\\"):
            where = "NAS (default)"
        elif p == str(APPDIR / "data" / "profiles"):
            where = "local — NAS not mounted"
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

    def _nas_autoconnect(self):
        """If NAS app creds are stored, authenticate before the library path is resolved (so the NAS
        default resolves on a PC with no drive mapped). Fully best-effort — never blocks startup hard."""
        try:
            from .config import get_nas_credentials, nas_connect
            if get_nas_credentials():
                nas_connect()
        except Exception:
            pass

    def nas_login_dialog(self):
        """Sign in to the NAS with the dedicated CAS app account (stored obfuscated in cas-config.json)."""
        from .config import get_nas_credentials, set_nas_credentials, nas_connect
        cur = get_nas_credentials() or ("", "")
        dlg = tk.Toplevel(self.win); dlg.title("NAS login")
        dlg.transient(self.win); dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=16); frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Sign in to the NAS with the CAS app account.\n"
                            "Saved in cas-config.json (obfuscated) so this PC auto-connects next launch.",
                  justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(frm, text="Username").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)
        uvar = tk.StringVar(value=cur[0])
        ttk.Entry(frm, textvariable=uvar, width=30).grid(row=1, column=1, sticky="w")
        ttk.Label(frm, text="Password").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=4)
        pvar = tk.StringVar(value=cur[1])
        ttk.Entry(frm, textvariable=pvar, width=30, show="•").grid(row=2, column=1, sticky="w")
        status = ttk.Label(frm, text="", foreground="#666")
        status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def save():
            set_nas_credentials(uvar.get().strip(), pvar.get())
            status.config(text="connecting…"); dlg.update_idletasks()
            ok = nas_connect()
            self.profiles_root = str(library_root())
            self._update_lib_label(); self.refresh_profiles()
            if ok or self._lib_reachable():
                self.log("NAS connected via the app account.")
                dlg.destroy()
            else:
                status.config(text="Saved, but couldn't connect — check the username/password and that "
                                   "192.168.100.227 is reachable.")
        btns = ttk.Frame(frm); btns.grid(row=4, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="Save & connect", command=save).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()

    # ---------- layout ----------
    def _build(self):
        top = ttk.Frame(self.win, padding=8)
        # `top` is packed LAST (end of this method), AFTER the footer + log — so those pin to the window
        # bottom and stay visible on a short / non-maximised window; `top` takes the space that's left.

        # Devices (left-top)
        devf = ttk.LabelFrame(top, text="Connected devices", padding=6)
        devf.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        self.dev_tree = ttk.Treeview(devf, columns=("model", "sd", "profile", "firmware", "state"),
                                     show="tree headings", height=7, selectmode="extended")
        self.dev_tree.heading("#0", text="serial")
        for c, t, w in (("model", "model", 115), ("sd", "SD card", 125),
                        ("profile", "profile", 105), ("firmware", "firmware", 140),
                        ("state", "state", 65)):
            self.dev_tree.heading(c, text=t)
            self.dev_tree.column(c, width=w)
        self.dev_tree.column("#0", width=120)
        self.dev_tree.pack(fill="both", expand=True)
        # Double-click a device row to ASSIGN the dropdown profile to it — quick manual override, works even
        # when auto-match said "(no match)" (e.g. several per-tier profiles share a model). MANUAL ALWAYS WINS.
        self.dev_tree.bind("<Double-1>", self._assign_on_doubleclick)
        self.dev_tree.bind("<<TreeviewSelect>>",
                           lambda e: (self._probe_sd_media(), self._update_fw_status()))  # SD box art + firmware
        devbtns = ttk.Frame(devf)
        devbtns.pack(anchor="w", fill="x", pady=(6, 0))
        _tip(ttk.Button(devbtns, text="Refresh devices", command=self.refresh_devices),
             "Re-scan for plugged-in devices and re-match each one to its profile by model.") \
            .pack(side="left")
        _tip(ttk.Button(devbtns, text="Assign profile → selected", command=self.assign_profile),
             "Set the profile (the one picked in the dropdown on the right) on the device row(s) selected "
             "in this list — Ctrl/Shift-click to pick several. OVERRIDES the model auto-match and sticks "
             "across refreshes. Tip: double-click a row to assign the dropdown profile to it fast.") \
            .pack(side="left", padx=(6, 0))

        # Profiles + manifest (right-top)
        prof = ttk.LabelFrame(top, text="Profile", padding=6)
        prof.grid(row=0, column=1, sticky="nsew", pady=(0, 6))
        row = ttk.Frame(prof)
        row.pack(fill="x")
        ttk.Label(row, text="Profile:").pack(side="left")
        self.prof_var = tk.StringVar()
        self.prof_combo = ttk.Combobox(row, textvariable=self.prof_var, state="readonly", width=24)
        self.prof_combo.pack(side="left", padx=4)
        self.prof_combo.bind("<<ComboboxSelected>>", lambda e: self.on_select_profile())
        _tip(ttk.Button(row, text="New…", command=self.new_profile),
             "Create a NEW empty profile (a saved-setup slot): give it a name and the device model it "
             "matches. Then plug in that family's master unit and use 'Save device → profile' to fill it.") \
            .pack(side="left", padx=2)
        _tip(ttk.Button(row, text="Delete…", command=self.delete_profile),
             "Delete this profile. It's ARCHIVED to profiles/_archive (recoverable) and you must type its "
             "exact name to confirm — nothing is permanently erased.").pack(side="left", padx=2)
        self.lib_var = tk.StringVar()
        ttk.Label(prof, textvariable=self.lib_var, foreground="#555").pack(anchor="w", pady=(2, 0))
        self._update_lib_label()
        # golden status for the selected profile: none saved, or size + estimated download time
        self.golden_var = tk.StringVar()
        ttk.Label(prof, textvariable=self.golden_var, foreground="#555").pack(anchor="w")

        # The rest of the panel lives in TABS so it isn't one tall wall of controls. The profile selector +
        # library + golden status above stay visible across all tabs.
        nb = ttk.Notebook(prof)
        nb.pack(fill="both", expand=True, pady=(6, 0))

        # ── Tab 1: Apps & options (the manifest) — the primary tab ──
        apps_tab = ttk.Frame(nb, padding=6)
        nb.add(apps_tab, text="Apps & options")
        _tip(ttk.Button(apps_tab, text="Save selection", command=self.save_manifest),
             "Save which apps and behavior options are ticked. This is exactly what 'Download' installs.") \
            .pack(side="bottom", anchor="w", pady=(6, 0))          # pinned below the scroll
        scrollwrap = ttk.Frame(apps_tab)
        scrollwrap.pack(side="top", fill="both", expand=True)
        _cv = tk.Canvas(scrollwrap, highlightthickness=0, borderwidth=0)
        _vsb = ttk.Scrollbar(scrollwrap, orient="vertical", command=_cv.yview)
        _cv.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side="right", fill="y")
        _cv.pack(side="left", fill="both", expand=True)
        self.modf = ttk.Frame(_cv)
        _mid = _cv.create_window((0, 0), window=self.modf, anchor="nw")
        self.modf.bind("<Configure>", lambda e: _cv.configure(scrollregion=_cv.bbox("all")))
        _cv.bind("<Configure>", lambda e: _cv.itemconfigure(_mid, width=e.width))

        def _wheel(e):                                    # cross-platform wheel: Win/Mac delta, X11 Button-4/5
            step = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
            _cv.yview_scroll(step, "units")
        _cv.bind("<Enter>", lambda e: (_cv.bind_all("<MouseWheel>", _wheel),
                                       _cv.bind_all("<Button-4>", _wheel), _cv.bind_all("<Button-5>", _wheel)))
        _cv.bind("<Leave>", lambda e: (_cv.unbind_all("<MouseWheel>"),
                                       _cv.unbind_all("<Button-4>"), _cv.unbind_all("<Button-5>")))

        # ── Tab 2: ES-DE box art (a BENCH setting persisted in cas-config.json) ──
        media_tab = ttk.Frame(nb, padding=8)
        nb.add(media_tab, text="ES-DE box art")
        self.media_mode = tk.StringVar(value="push" if config.es_media_src() else "sd")
        self.media_path = tk.StringVar(value=config.es_media_src() or "")
        ttk.Radiobutton(media_tab, text="Use the SD card  (no transfer — box art rides the SD image)",
                        value="sd", variable=self.media_mode, command=self._on_media_mode).pack(anchor="w")
        prow = ttk.Frame(media_tab)
        prow.pack(fill="x", pady=(6, 0))
        ttk.Radiobutton(prow, text="Push from PC folder:", value="push",
                        variable=self.media_mode, command=self._on_media_mode).pack(side="left")
        ttk.Entry(prow, textvariable=self.media_path, width=30).pack(side="left", padx=(4, 0))
        ttk.Button(prow, text="Browse…", command=self._browse_media).pack(side="left", padx=4)
        self.sd_media_var = tk.StringVar(value="")    # auto-detected SD status (filled on refresh/select)
        ttk.Label(media_tab, textvariable=self.sd_media_var, foreground="#555",
                  wraplength=440, justify="left").pack(anchor="w", pady=(10, 0))

        # ── Tab 3: Root images (for ⓪ Root) — one bundled kit; stock init_boot is the per-family override ──
        root_tab = ttk.Frame(nb, padding=8)
        nb.add(root_tab, text="Root images")
        self.stock_var = tk.StringVar()
        srow = ttk.Frame(root_tab)
        srow.pack(fill="x")
        ttk.Label(srow, text="Stock init_boot:").pack(side="left")
        ttk.Entry(srow, textvariable=self.stock_var, state="readonly", width=28).pack(side="left", padx=(4, 0))
        _tip(ttk.Button(srow, text="Browse…", command=self._browse_stock_init_boot),
             "Override this profile's STOCK init_boot. Blank = the bundled default kit image. ⓪ Root patches "
             "it with Magisk ON the device and flashes it. Use the unit's OWN firmware image; a different "
             "model's / SPL's init_boot can bootloop (recoverable on an unlocked unit).").pack(side="left", padx=4)
        ttk.Label(root_tab, text=f"Default kit (all profiles): {P.pathlib.Path(PV.DEFAULT_STOCK_INIT_BOOT).name}"
                                 f" + {P.pathlib.Path(PV.DEFAULT_MAGISK_APK).name}",
                  foreground="#555", wraplength=440, justify="left").pack(anchor="w", pady=(10, 0))

        # ── Firmware library (DEVICE ROOT firmware: the full OS/boot build per device; library-only —
        #    CAS stores + suggests it, it does NOT flash. Distinct from emulator BIOS.) ──
        ttk.Separator(root_tab, orient="horizontal").pack(fill="x", pady=(12, 8))
        ttk.Label(root_tab, text="Firmware library (device root firmware)",
                  font=("", 9, "bold")).pack(anchor="w")
        fwrow = ttk.Frame(root_tab)
        fwrow.pack(fill="x", pady=(4, 0))
        ttk.Label(fwrow, text="Firmware:").pack(side="left")
        self.fw_var = tk.StringVar()
        self.fw_combo = ttk.Combobox(fwrow, textvariable=self.fw_var, state="readonly", width=22)
        self.fw_combo.pack(side="left", padx=4)
        _tip(ttk.Button(fwrow, text="Assign → selected", command=self.assign_firmware),
             "Set this firmware on the selected device row(s) as a sticky MANUAL override (always wins over "
             "the serial-prefix auto-match). Remembered across launches.").pack(side="left", padx=2)
        _tip(ttk.Button(fwrow, text="Add / update…", command=self._add_firmware),
             "Ingest a raw firmware BUILD FOLDER into the library as a new version (auto-detects device / "
             "storage / flash target, keeps history). Pick the folder containing the emmc/ or ufs/ payload.") \
            .pack(side="left", padx=2)
        self.fw_status_var = tk.StringVar(value="Select a device to see its firmware suggestion.")
        ttk.Label(root_tab, textvariable=self.fw_status_var, foreground="#555",
                  wraplength=440, justify="left").pack(anchor="w", pady=(6, 0))
        self.refresh_firmware()

        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)
        top.rowconfigure(0, weight=1)

        # Action area: ONE "apply to ALL connected" toggle + the workflow buttons in order:
        # Root -> Save a master -> Download -> Lock. Each device carries its OWN assigned profile (the
        # "profile" column; auto-matched by model, or hand-set via "Assign profile → selected"). Root,
        # Download and Lock run on the SELECTED row(s) — or EVERY connected device when the toggle is on —
        # IN PARALLEL, each using its assigned profile. Only Save is always one device.
        # Footer pinned to the window BOTTOM so the action buttons stay visible even on a short window —
        # the devices/profile/log areas above shrink instead of pushing the buttons off-screen (the cause
        # of the "buttons have no text" clip on small screens).
        footer = ttk.Frame(self.win)
        footer.pack(side="bottom", fill="x")
        act = ttk.Frame(footer, padding=(8, 0))
        act.pack(side="top", fill="x")
        self.batch_var = tk.BooleanVar(value=False)
        _tip(ttk.Checkbutton(act, text="Apply to ALL connected devices  (else: the selected row(s))",
                             variable=self.batch_var, command=self._on_batch_toggle),
             "OFF: Root / Download / Lock run on the device ROW(S) you select (Ctrl/Shift-click for "
             "several).\n"
             "ON: they run on EVERY connected device.\n"
             "Either way they run IN PARALLEL, and EACH device uses its OWN assigned profile (the "
             "'profile' column). The golden is never sealed.") \
            .pack(anchor="w", pady=(2, 2))
        row2 = ttk.Frame(act)
        row2.pack(fill="x")
        self.btns = []
        for text, cmd, tip in (
            ("⓪ Root", self.root_device,
             "Root the selected device(s) — or ALL connected if the toggle is on — with Magisk, sourced "
             "entirely from the PC: flashes each device's ASSIGNED profile's Magisk-patched init_boot, then "
             "installs the Magisk app FROM THE PC (not the SD). Bootloaders must be UNLOCKED; units reboot a "
             "couple of times. Runs IN PARALLEL. (Inverse of 'Lock'.)"),
            ("① Save device → profile", self.capture_update,
             "SAVE what's on the selected device INTO a profile on this PC (the master 'golden'). Always "
             "ONE device. The previous version is kept as .prev for rollback. Direction: device → PC."),
            ("② Download", self.provision_selected,
             "DOWNLOAD each device's ASSIGNED profile onto it — installs every ticked app plus its "
             "saves/BIOS/keys, settings, folder permissions, and homescreen layout (replaces current app "
             "data). Direction: PC → device. Runs on the selected device(s), or ALL connected if the toggle "
             "is on, IN PARALLEL. (Formerly 'Provision'.)"),
            ("③ Lock for shipping", self.seal_device,
             "Final step on VERIFIED unit(s): HIDES Developer options, removes root (Magisk), and disables "
             "USB debugging so it's retail-ready (adb disconnects). Runs on the selected device(s), or ALL "
             "connected if the toggle is on, IN PARALLEL — the golden is skipped. (Inverse of 'Root'.)"),
        ):
            b = ttk.Button(row2, text=text, command=cmd)
            b.pack(side="left", padx=4, pady=4)
            _tip(b, tip)
            self.btns.append(b)

        # Activity bar: an animated progress strip + live status/elapsed so long jobs (e.g. pulling a
        # multi-GB golden over USB) visibly show they're WORKING, not frozen.
        statusf = ttk.Frame(footer, padding=(8, 2))
        statusf.pack(side="top", fill="x")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(statusf, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(statusf, mode="indeterminate", length=210)
        self.progress.pack(side="right", padx=4)

        # Log — a compact pane pinned just above the footer. Packed BEFORE `top` (below) so footer + log
        # always stay on screen; `top` takes the rest and its app list scrolls when the window is short.
        logf = ttk.LabelFrame(self.win, text="Log", padding=6)
        logf.pack(side="bottom", fill="both", expand=False, padx=8, pady=(0, 8))
        self.logbox = scrolledtext.ScrolledText(logf, height=6, state="disabled", wrap="word")
        self.logbox.pack(fill="both", expand=True)

        # finally place the top area — LAST, so the bottom-pinned footer + log reserve their space first.
        top.pack(side="top", fill="both", expand=True)

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
        self.log(f"⏱ starting: {label}")
        for b in self.btns:
            b.configure(state="disabled")
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
            self._report(self._action, result_box.get("r"))
            for b in self.btns:
                b.configure(state="normal")
            try:
                self.win.configure(cursor="")
            except tk.TclError:
                pass
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

    def _report(self, label, result):
        """Post-action mini-report in the log — a clear pass / skip / fail summary for easy debugging.
        Accepts a batch dict {serial: (status, detail)}, a single bool, or None (nothing to summarize)."""
        DONE = ("ok",)
        SKIP = ("skip", "skip-golden", "no-profile", "no-init_boot")
        if isinstance(result, dict):
            good = [s for s, (st, _) in result.items() if st in DONE]
            skipped = [s for s, (st, _) in result.items() if st in SKIP]
            bad = [s for s, (st, _) in result.items() if st not in DONE and st not in SKIP]
            self.log(f"──────── REPORT: {label} ────────")
            for s, (st, d) in result.items():
                mark = "✅" if st in DONE else ("⏭" if st in SKIP else "❌")
                self.log(f"   {mark} {s}: {st}" + (f" — {d}" if d else ""))
            summary = f"   → {len(good)} ok"
            if skipped:
                summary += f", {len(skipped)} skipped"
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
        self._update_lib_label()
        if not self._lib_reachable():
            self.log(f"Library not reachable: {self.profiles_root} — is the NAS drive mapped? "
                     "Use 'Library…' to fix the path.")
        names = [p.name for p in P.list_profiles(self.profiles_root)]
        self.prof_combo["values"] = names
        if names and self.prof_var.get() not in names:
            self.prof_var.set(names[0])
        self.on_select_profile()

    def on_select_profile(self):
        for w in self.modf.winfo_children():
            w.destroy()
        self.pkg_vars = {}
        self._icon_refs = []                 # keep PhotoImage refs alive (Tk GCs unreferenced images)
        self._update_golden_status()         # golden: none / size + download ETA
        name = self.prof_var.get()
        if not name:
            return
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
        self.stock_var.set(prof.meta.get("stock_init_boot", ""))     # blank = the bundled default kit image
        included = set(prof.pkgs())
        # "Select all" — toggles every app checkbox below at once; reflects all-on/all-off live.
        self.selall_var = tk.BooleanVar(value=False)
        _tip(ttk.Checkbutton(self.modf, text="  Select all apps", variable=self.selall_var,
                             command=self._toggle_all_apps),
             "Tick or untick every app in this list at once. (Behaviour options below are unaffected.)") \
            .pack(anchor="w")
        ttk.Separator(self.modf, orient="horizontal").pack(fill="x", pady=(2, 4))
        for pkg in prof.all_pkgs():
            var = tk.BooleanVar(value=(pkg in included))
            self.pkg_vars[pkg] = var
            cb = ttk.Checkbutton(self.modf, text=f" {_app_label(pkg)}", variable=var,
                                 command=self._sync_selall)
            # real launcher icon from the app's bundled APK, else a uniform placeholder so rows align
            icon = self._app_icon(prof, pkg) or self._placeholder_icon(_app_label(pkg))
            if icon is not None:
                cb.configure(image=icon, compound="left")
            _tip(cb, f"Package: {pkg}").pack(anchor="w")   # exact package id on hover (name is friendly)
        self._sync_selall()                                # set the master box from the initial selection
        # behavior flags: @settings/@hardening/@grants/@homescreen honored by restore.sh on the device.
        # The GameCove Companion is a normal golden app (ticked in the list above), not a behavior flag.
        self.flag_vars = {}
        flags = prof.flags()
        flag_labels = {"settings": "Display & system settings", "hardening": "Performance & update lock",
                       "grants": "Folder permissions", "homescreen": "Homescreen layout"}
        flag_tips = {
            "settings": "Apply the saved display/brightness/animation/screen-timeout preferences.",
            "hardening": "Keep emulators awake (exempt from battery optimization so they're never killed) "
                         "and block OTA system updates that could break root.",
            "grants": "Restore folder-access permissions so ES-DE and the emulators can read your "
                      "ROM/BIOS folders without re-asking on first launch.",
            "homescreen": "Restore the homescreen layout — your app folders, icon/dock arrangement, "
                          "wallpaper (and widgets, best-effort).",
        }
        ttk.Label(self.modf, text="— behavior —").pack(anchor="w", pady=(6, 0))
        for fl in ("settings", "hardening", "grants", "homescreen"):
            fv = tk.BooleanVar(value=(flags.get(fl, "on") == "on"))
            self.flag_vars[fl] = fv
            cb = ttk.Checkbutton(self.modf, text=f"{flag_labels.get(fl, fl)}  (@{fl})", variable=fv)
            _tip(cb, flag_tips.get(fl, "")).pack(anchor="w")

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
            try:
                fw_root = FW.firmware_root()
            except Exception:
                fw_root = None
            for serial, state in devs:
                model, sd, auto = "", "", ""
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
                    if fw_root is not None:
                        try:
                            fwmap[serial] = FW.resolve(serial, FW.identity(a), fw_root)
                        except Exception as e:
                            fwmap[serial] = {"firmware_id": None, "version": None, "manual": False,
                                             "ok": False, "warnings": [f"error: {e}"], "firmware": None}
                rows.append((serial, model, sd, auto, state))
            self.win.after(0, lambda r=rows, fm=fwmap: self._populate_devices(r, fm))
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

    def _populate_devices(self, rows, fwmap=None):
        """(UI thread) fill the device tree (keyed by adb serial):
          * MANUAL assignment  -> sticky + remembered across launches; the operator's choice always wins.
          * AUTO assignment    -> FOLLOWS the live match every refresh, so if something CHANGES (SD card
                                  swapped to a different tier, a better-matching profile added) the device
                                  re-auto-assigns itself.
        Hand-assigned rows are tinted green."""
        self.fw_resolved = fwmap or {}
        self.dev_tree.delete(*self.dev_tree.get_children())
        changes = []                                       # auto-matches that CHANGED for a known device
        for serial, model, sd, auto, state in rows:
            if serial in self.assigned_manual:
                shown = self.assigned.get(serial, auto)    # operator override: locked + remembered
            else:
                shown = auto                               # auto: always reflect the current best match
                prev = self._last_auto.get(serial)
                if prev is not None and prev != auto:      # known device whose auto-match just changed
                    changes.append((serial, prev, auto))
                self._last_auto[serial] = auto
                self.assigned[serial] = auto
            tags = ("manual",) if serial in self.assigned_manual else ()
            self.dev_tree.insert("", "end", iid=serial, text=serial,
                                 values=(model, sd, shown, self._fw_cell(serial), state), tags=tags)
        self.dev_tree.tag_configure("manual", foreground="#1a6f1a")
        self.log("refreshed: " + ", ".join(f"{r[0]} → {self.assigned.get(r[0], '?')}" for r in rows)
                 if rows else "refreshed: 0 device(s)")
        self._probe_sd_media()                             # auto-detect ES-DE/box art on the connected SD
        if changes:                                        # inform the operator an auto-match changed (e.g. SD swap)
            body = "\n".join(f"  {s}:  {old}  →  {new}" for s, old, new in changes)
            messagebox.showinfo(
                "CAS — auto-match updated",
                f"A device's auto-matched profile changed (SD card or library change):\n\n{body}\n\n"
                "Kept automatically. Use 'Assign profile → selected' (or double-click a row) to override.")

    # ---------- actions ----------
    def _selected_serial(self):
        sel = self.dev_tree.selection()
        return sel[0] if sel else None

    def save_manifest(self):
        name = self.prof_var.get()
        if not name:
            return
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
        pkgs = [p for p, v in self.pkg_vars.items() if v.get()]
        flags = {fl: ("on" if v.get() else "off") for fl, v in self.flag_vars.items()}
        P.save_manifest(prof.manifest_path, pkgs, flags, header=f"# {name}")
        self.log(f"saved manifest for {name}: {len(pkgs)} module(s), flags={flags}")

    def _on_batch_toggle(self):
        if self.batch_var.get():
            self.status_var.set("Apply-to-ALL ON — Root, Download & Lock run on EVERY connected device IN "
                                "PARALLEL, each with its own assigned profile. Only Save is one device.")
        else:
            self.status_var.set("Selection mode — actions run on the device row(s) you select "
                                "(Ctrl/Shift-click for several).")

    def _selected_profile(self):
        name = self.prof_var.get()
        return P.Profile(P.pathlib.Path(self.profiles_root) / name) if name else None

    def _selected_serials(self):
        return list(self.dev_tree.selection())

    def _action_targets(self):
        """Serials an action runs on: ALL connected if 'Apply to ALL' is ticked, else the selected row(s).
        Returns None (after a message) when nothing is targeted."""
        serials = (list(self.dev_tree.get_children()) if self.batch_var.get()
                   else list(self.dev_tree.selection()))
        if not serials:
            messagebox.showinfo("CAS", "Select one or more device rows (Ctrl/Shift-click), or tick "
                                       "'Apply to ALL connected devices'.")
            return None
        return serials

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
        """Double-click a device row → assign the dropdown profile to THAT row (fast manual override)."""
        row = self.dev_tree.identify_row(event.y)
        if not row:
            return
        if not self.prof_var.get():
            messagebox.showinfo("CAS", "Pick a profile in the dropdown on the right first, then "
                                       "double-click a device row to assign it.")
            return
        self.dev_tree.selection_set(row)        # assign_profile works on the current selection
        self.assign_profile()

    def assign_profile(self):
        """Assign the dropdown profile to the selected device row(s), with a Yes/No confirm (+ a warning if
        the profile's model_match doesn't fit a selected device)."""
        serials = list(self.dev_tree.selection())
        if not serials:
            messagebox.showinfo("CAS", "Select one or more device rows first (Ctrl/Shift-click).")
            return
        name = self.prof_var.get()
        if not name:
            messagebox.showinfo("CAS", "Pick a profile in the dropdown on the right first.")
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
            config.set_device_profile(s, name, manual=True)   # remember across launches (sticky override)
            if self.dev_tree.exists(s):
                vals = list(self.dev_tree.item(s).get("values") or ["", "", "", "", ""])
                vals[2] = name
                self.dev_tree.item(s, values=vals, tags=("manual",))
        self.log(f"assigned profile '{name}' to: {', '.join(serials)} (remembered for next time)")

    # ---------- firmware library (DEVICE ROOT firmware; library-only — never flashes) ----------
    def _fw_cell(self, serial):
        """The 'firmware' column text for a device row, from its resolved suggestion/override."""
        r = self.fw_resolved.get(serial)
        if r is None:
            return ""
        if not r.get("firmware_id"):
            return "(no match)"
        return r["firmware_id"] + ("" if r.get("ok") else " ⚠")

    def refresh_firmware(self):
        """Populate the firmware dropdown with every firmware id in the library."""
        try:
            ids = [f.id for f in FW.list_firmware(FW.firmware_root())]
        except Exception:
            ids = []
        self.fw_combo["values"] = ids
        if ids and self.fw_var.get() not in ids:
            self.fw_var.set(ids[0])

    def _update_fw_status(self):
        """Show the selected device's resolved firmware + logic-check + payload path under the dropdown."""
        serial = self._selected_serial()
        if not serial:
            self.fw_status_var.set("Select a device to see its firmware suggestion.")
            return
        r = self.fw_resolved.get(serial)
        if not r:
            self.fw_status_var.set(f"{serial}: no firmware info — click 'Refresh devices'.")
            return
        if not r.get("firmware_id"):
            self.fw_status_var.set(f"{serial}: no match in library — pick one and 'Assign → selected'.")
            return
        kind = "manual override" if r.get("manual") else "auto-suggested"
        head = f"{serial}: {r['firmware_id']}  v{r.get('version') or '?'}  ({kind})"
        check = "✓ logic-check OK" if r.get("ok") else "⚠ " + "; ".join(r.get("warnings") or ["mismatch"])
        path = ""
        fw = r.get("firmware")
        try:
            pd = fw.payload_dir(r.get("version")) if fw is not None else None
            path = f"\npayload: {pd}" if pd else ""
        except Exception:
            path = ""
        self.fw_status_var.set(f"{head}\n{check}{path}")

    def assign_firmware(self):
        """Assign the dropdown firmware to the selected device row(s) as a sticky MANUAL override."""
        serials = list(self.dev_tree.selection())
        if not serials:
            messagebox.showinfo("CAS", "Select one or more device rows first (Ctrl/Shift-click).")
            return
        fid = self.fw_var.get()
        if not fid:
            messagebox.showinfo("CAS", "Pick a firmware in the dropdown (Root images tab) first. "
                                       "Use 'Add / update…' if the library is empty.")
            return
        if not messagebox.askyesno(
                "CAS — assign firmware",
                f"Set firmware '{fid}' on {len(serials)} device(s) as a manual override?\n  "
                + "\n  ".join(serials)):
            return
        for s in serials:
            config.set_device_firmware(s, fid, manual=True)   # sticky; always wins over the auto-match
            FW.log_event(s, fid, None, "assign", True)
        self.log(f"assigned firmware '{fid}' to: {', '.join(serials)} (remembered). Re-resolving…")
        self.refresh_devices()                                # re-resolve so the column + status reflect it

    def _add_firmware(self):
        """Ingest a raw firmware build folder into the library (new version) on a background thread."""
        folder = filedialog.askdirectory(title="Select a firmware build folder (contains emmc/ or ufs/)")
        if not folder:
            return

        def work():
            fw = FW.ingest(folder, FW.firmware_root())
            self.log(f"firmware ingested: {fw.id}  v{fw.current()}")
            self.win.after(0, self.refresh_firmware)
            self.win.after(0, self.refresh_devices)
            return True
        self._run_bg(work, label="Ingesting firmware")

    def _run_batch(self, kind, serials, devices=None):
        """Run kind ∈ {download, root, lock} on `serials`, each with its ASSIGNED profile, IN PARALLEL —
        with a confirm (first run), the mini-report, and per-failure retry. `devices` is set on a retry."""
        first = devices is None
        pm, force = self._profile_map(serials)
        if first:
            verb = {"download": "Download to", "root": "Root", "lock": "Seal (lock)"}[kind]
            lines = "\n  ".join(f"{s} → {self.assigned.get(s) or '(no profile)'}" for s in serials)
            extra = {"root": "\n\nBootloaders must be UNLOCKED; each device reboots a couple of times.",
                     "lock": "\n\nAssumes each unit is VERIFIED. Hides Developer options, un-roots, and "
                             "disables USB debugging. The golden is skipped.",
                     "download": ""}[kind]
            if not messagebox.askyesno(
                    f"CAS — {verb} {len(serials)} device(s)",
                    f"{verb} these device(s), each with its own profile? They run IN PARALLEL.\n  "
                    + lines + extra):
                return
        devs = devices if devices is not None else [(s, "device") for s in serials]

        def work():
            if kind == "download":
                res = PV.provision_all(lambda s: Adb(serial=s, adb=self.adb_bin), devs,
                                       root=self.profiles_root, log=self.log, profile_map=pm,
                                       es_media_src=config.es_media_src())
            elif kind == "root":
                res = PV.root_all(lambda s: Adb(serial=s, adb=self.adb_bin),
                                  lambda s: Fastboot(serial=s, fastboot=self.fb_bin), devs,
                                  profiles_root=self.profiles_root, appdir=APPDIR, log=self.log,
                                  profile_map=pm, force_serials=force)
            else:  # lock
                res = PV.seal_all(lambda s: Adb(serial=s, adb=self.adb_bin),
                                  lambda s: Fastboot(serial=s, fastboot=self.fb_bin), devs,
                                  profiles_root=self.profiles_root, appdir=APPDIR, log=self.log,
                                  profile_map=pm, force_serials=force)
            self.win.after(0, self.refresh_devices)
            failed = [s for s, v in res.items() if v[0] in ("fail", "error")]
            if failed:
                self._retry_ctx = (
                    f"{len(failed)} device(s) failed {kind}:\n  {', '.join(failed)}\n\nRetry just those?",
                    lambda fs=failed: self._run_batch(kind, fs, devices=[(s, "device") for s in fs]))
            return res
        label = {"download": "Downloading", "root": "Rooting", "lock": "Locking"}[kind]
        self._run_bg(work, label=f"{label} {len(serials)} device(s)"
                                 f"{' (retry)' if devices is not None else ''}")

    def provision_selected(self):
        t = self._action_targets()
        if t:
            self._run_batch("download", t)

    def capture_update(self):
        serial = self._selected_serial()
        if not serial:
            messagebox.showinfo("CAS", "Select the GOLDEN device in the list first.")
            return
        name = simpledialog.askstring("Capture / Update", "Profile name to capture into:",
                                      initialvalue=self.prof_var.get())
        if not name:
            return
        # if the target profile already holds a golden, this OVERWRITES it -> confirm; else just save.
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
        if prof.has_golden():
            if not messagebox.askyesno(
                    "CAS — overwrite saved golden?",
                    f"Profile '{name}' already has a saved golden ({_human_size(prof.golden_size())}).\n\n"
                    f"Overwrite it with device {serial}?\n(the current payload is kept as .prev for rollback)"):
                return
        elif not messagebox.askyesno(
                "CAS — save golden",
                f"Save device {serial} into profile '{name}'?\n(no golden saved there yet)"):
            return
        def work():
            ok = PV.capture_to_pc(Adb(serial=serial, adb=self.adb_bin), name, _stamp(),
                                  root=self.profiles_root, log=self.log)
            self.win.after(0, self.refresh_profiles)
            return ok
        self._run_bg(work, label=f"Saving {serial} → {name}")

    def root_device(self):
        t = self._action_targets()
        if t:
            self._run_batch("root", t)

    def seal_device(self):
        t = self._action_targets()
        if t:
            self._run_batch("lock", t)

    def _lib_reachable(self):
        """Is the CURRENTLY-SELECTED library path (self.profiles_root) a reachable directory?
        (Checks the cached path the UI actually lists — consistent with refresh_profiles.)"""
        try:
            return P.pathlib.Path(self.profiles_root).is_dir()
        except OSError:
            return False

    def _update_lib_label(self):
        root = self.profiles_root
        mark = "✓" if self._lib_reachable() else "✗ not reachable (map the NAS drive?)"
        self.lib_var.set(f"Library: {root}   {mark}")

    def _update_golden_status(self):
        """Show the selected profile's golden: none saved, or its size + an estimated download time
        (averaged from past Downloads). Sizing runs off-thread so a NAS library never freezes the UI."""
        if not hasattr(self, "golden_var"):
            return
        name = self.prof_var.get()
        if not name:
            self.golden_var.set("")
            return
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
        if not prof.has_golden():
            self.golden_var.set("Golden: none saved yet — use '① Save device → profile' to capture one.")
            return
        self.golden_var.set("Golden: saved · sizing…")

        def work():
            b = prof.golden_size()
            mbps = config.download_mbps(prof.name)        # prefer this profile's own download history
            if mbps and b:
                eta = f" · ~{_human_eta((b / 1048576.0) / mbps)} to download (avg {mbps:.0f} MB/s)"
            else:
                eta = " · download time estimated after the first Download"
            txt = f"Golden: saved · {_human_size(b)}{eta}"
            self.win.after(0, lambda: self.golden_var.set(txt))
        threading.Thread(target=work, daemon=True).start()

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

    def _store_path(self, p):
        """How a picked Root image is recorded in profile.meta: APPDIR-relative when it lives inside the app
        bundle (portable across machines + the shared NAS profile library), else the absolute path. Both
        forms resolve via profiles.resolve_asset at root time."""
        p = P.pathlib.Path(p).resolve()
        try:
            return str(p.relative_to(P.pathlib.Path(APPDIR).resolve()))
        except ValueError:
            return str(p)

    def _set_profile_asset(self, key, title, filetypes, var):
        """Pick a Root image (stock init_boot / Magisk apk) and write it into the SELECTED profile's
        profile.meta. Persists per-profile so ⓪ Root can find it; the device list isn't touched."""
        name = self.prof_var.get()
        if not name:
            messagebox.showinfo("CAS", "Select a profile first.")
            return
        f = filedialog.askopenfilename(title=title, filetypes=filetypes)
        if not f:
            return
        val = self._store_path(f)
        P.set_meta_key(P.pathlib.Path(self.profiles_root) / name / "profile.meta", key, val)
        var.set(val)
        self.log(f"profile '{name}': set {key} = {val}")

    def _browse_stock_init_boot(self):
        self._set_profile_asset("stock_init_boot", "Pick the device family's STOCK init_boot (.img)",
                                [("init_boot image", "*.img"), ("all files", "*.*")], self.stock_var)

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
                out = a.shell("ls -d /storage/*-*/ES-DE /storage/*-*/ES-DE/downloaded_media "
                              "/storage/*-*/downloaded_media 2>/dev/null")[1].strip()
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

    def _toggle_all_apps(self):
        """'Select all apps' clicked — set every app checkbox to the master box's state."""
        on = self.selall_var.get()
        for v in self.pkg_vars.values():
            v.set(on)

    def _sync_selall(self):
        """Keep the 'Select all apps' box in sync: ticked only when every app is ticked."""
        if hasattr(self, "selall_var"):
            self.selall_var.set(bool(self.pkg_vars) and all(v.get() for v in self.pkg_vars.values()))

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
        # No regex needed — the profile auto-matches by NAME similarity + SD size. Put the device model and
        # the SD capacity in the name (e.g. 'retroid-pocket-6-512', 'retroid-pocket-6-256') and CAS matches
        # a Retroid Pocket 6 with a ~512 GB card to the -512 profile, a ~256 GB card to the -256 profile.
        name = simpledialog.askstring(
            "New profile",
            "New profile name — include the device model and SD size so it auto-matches, e.g.:\n"
            "   retroid-pocket-6-512   ·   retroid-pocket-6-256   ·   odin2-mini")
        if not name:
            return
        d = P.pathlib.Path(self.profiles_root) / name
        if d.exists():
            messagebox.showerror("CAS", "A profile with that name already exists.")
            return
        d.mkdir(parents=True)
        # model_match left blank on purpose — name-similarity handles it. (Set it by hand only for odd model
        # strings the name can't capture; an explicit model_match still takes precedence.)
        (d / "profile.meta").write_text("model_match=\nfrontend=\nnotes=\ncaptured=\n")
        (d / "manifest").write_text(f"# {name} (empty — capture a golden to populate)\n")
        self.log(f"created profile '{name}' — auto-matches by name + SD size, no regex needed. "
                 "Select the golden device, then 'Save device → profile'.")
        self.refresh_profiles()
        self.prof_var.set(name)
        self.on_select_profile()

    def delete_profile(self):
        name = self.prof_var.get()
        if not name:
            return
        # deliberately hard: require typing the exact name. Archives (moves), never rm.
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


def main(adb_bin="adb", fb_bin="fastboot"):
    win = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    try:    # GameCove logo in the titlebar/taskbar (keep a ref on win so Tk doesn't GC the image)
        win._cas_icon = tk.PhotoImage(file=str(BUNDLE / "assets" / "cas-window.png"))
        win.iconphoto(True, win._cas_icon)
    except Exception:
        pass
    App(win, adb_bin=adb_bin, fb_bin=fb_bin)
    win.mainloop()


if __name__ == "__main__":
    main()
