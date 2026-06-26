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
from .adb import Adb, Fastboot, list_devices
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
        self.assigned = {}          # serial -> profile name (per-device; defaults to the model auto-match)
        self.assigned_manual = set()  # serials whose profile was set by hand (deliberate; allows force)
        win.title("CAS — Console Auto Setup")
        win.geometry("1000x720")
        win.minsize(820, 470)       # keep the action bar reachable even when shrunk
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
        """Background check against the public GitHub Release. manual=True also reports 'up to date'."""
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
        if not explicit and target == str(APPDIR / "profiles"):
            target = NAS_DEFAULT          # default fell back to local — take the user to the NAS instead
        if sys.platform != "win32" and target.startswith("\\\\"):
            target = "smb://" + target[2:].replace("\\", "/")   # \\host\share\.. -> smb://host/share/..
        if not self._open_path(target):
            messagebox.showwarning(
                "CAS",
                f"Couldn't open a file manager for:\n{target}\n\n"
                "On Windows this opens in Explorer. On this machine, open it manually in your file "
                "manager (paste the address above).")

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
        elif p == str(APPDIR / "profiles"):
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
        top.pack(fill="both", expand=True)

        # Devices (left-top)
        devf = ttk.LabelFrame(top, text="Connected devices", padding=6)
        devf.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        self.dev_tree = ttk.Treeview(devf, columns=("model", "sd", "profile", "state"),
                                     show="tree headings", height=7, selectmode="extended")
        self.dev_tree.heading("#0", text="serial")
        for c, t, w in (("model", "model", 120), ("sd", "SD card", 135),
                        ("profile", "profile", 110), ("state", "state", 70)):
            self.dev_tree.heading(c, text=t)
            self.dev_tree.column(c, width=w)
        self.dev_tree.column("#0", width=120)
        self.dev_tree.pack(fill="both", expand=True)
        devbtns = ttk.Frame(devf)
        devbtns.pack(anchor="w", fill="x", pady=(6, 0))
        _tip(ttk.Button(devbtns, text="Refresh devices", command=self.refresh_devices),
             "Re-scan for plugged-in devices and re-match each one to its profile by model.") \
            .pack(side="left")
        _tip(ttk.Button(devbtns, text="Assign profile → selected", command=self.assign_profile),
             "Set the profile (the one picked in the dropdown on the right) on the device row(s) selected "
             "in this list — Ctrl/Shift-click to pick several. Asks to confirm. Each device keeps its own "
             "profile; actions use it.") \
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

        _tip(ttk.Label(prof, text="Apps & options to include:"),
             "Tick the apps/emulators this profile installs, plus the behavior options below. "
             "Untick anything you want to leave OFF for this setup.").pack(anchor="w", pady=(6, 0))
        self.modf = ttk.Frame(prof)
        self.modf.pack(fill="both", expand=True)
        _tip(ttk.Button(prof, text="Save selection", command=self.save_manifest),
             "Save which apps and behavior options are ticked above. This is exactly what "
             "'Download to device' will install/apply.").pack(anchor="w", pady=(4, 0))

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

        # Log — fills the middle, ABOVE the pinned footer; shrinks first when the window is small.
        logf = ttk.LabelFrame(self.win, text="Log", padding=6)
        logf.pack(side="bottom", fill="both", expand=True, padx=8, pady=8)
        self.logbox = scrolledtext.ScrolledText(logf, height=8, state="disabled", wrap="word")
        self.logbox.pack(fill="both", expand=True)

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
        name = self.prof_var.get()
        if not name:
            return
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name)
        included = set(prof.pkgs())
        for pkg in prof.all_pkgs():
            var = tk.BooleanVar(value=(pkg in included))
            self.pkg_vars[pkg] = var
            ttk.Checkbutton(self.modf, text=pkg, variable=var).pack(anchor="w")
        # behavior flags: @settings/@hardening/@grants/@homescreen honored by restore.sh on the device;
        # @companion honored by the PC provision step (installs the Companion app off the PC after restore).
        self.flag_vars = {}
        flags = prof.flags()
        flag_labels = {"settings": "Display & system settings", "hardening": "Performance & update lock",
                       "grants": "Folder permissions", "homescreen": "Homescreen layout",
                       "companion": "Install GameCove Companion app"}
        flag_tips = {
            "settings": "Apply the saved display/brightness/animation/screen-timeout preferences.",
            "hardening": "Keep emulators awake (exempt from battery optimization so they're never killed) "
                         "and block OTA system updates that could break root.",
            "grants": "Restore folder-access permissions so ES-DE and the emulators can read your "
                      "ROM/BIOS folders without re-asking on first launch.",
            "homescreen": "Restore the homescreen layout — your app folders, icon/dock arrangement, "
                          "wallpaper (and widgets, best-effort).",
            "companion": "Install the GameCove Companion app from the PC after restore "
                         "(Apps/gamecove-companion.apk). Untick to leave it off for this setup.",
        }
        ttk.Label(self.modf, text="— behavior —").pack(anchor="w", pady=(6, 0))
        for fl in ("settings", "hardening", "grants", "homescreen", "companion"):
            fv = tk.BooleanVar(value=(flags.get(fl, "on") == "on"))
            self.flag_vars[fl] = fv
            _tip(ttk.Checkbutton(self.modf, text=f"{flag_labels.get(fl, fl)}  (@{fl})", variable=fv),
                 flag_tips.get(fl, "")).pack(anchor="w")

    def refresh_devices(self):
        self.dev_tree.delete(*self.dev_tree.get_children())

        def work():
            try:
                devs = list_devices(adb=self.adb_bin)
            except Exception as e:
                self.log(f"adb error: {e} (is adb on PATH?)")
                return
            rows = []
            for serial, state in devs:
                model, sd, auto = "", "", ""
                if state == "device":
                    a = Adb(serial=serial, adb=self.adb_bin)
                    model = a.getprop("ro.product.model")
                    m = P.match_profile(model, self.profiles_root)
                    auto = m.name if m else "(no match)"
                    try:
                        sd = a.sd_info()        # SD serial + size (or 'no SD') — catches wrong/missing cards
                    except Exception:
                        sd = "?"
                rows.append((serial, model, sd, auto, state))
            self.win.after(0, lambda r=rows: self._populate_devices(r))
        threading.Thread(target=work, daemon=True).start()

    def _populate_devices(self, rows):
        """(UI thread) fill the device tree + reconcile per-device profile assignments. A device with no
        hand-set profile tracks its live model auto-match; manual assignments are preserved; gone devices
        are forgotten. Hand-assigned rows are tinted green."""
        self.dev_tree.delete(*self.dev_tree.get_children())
        present = {r[0] for r in rows}
        for s in list(self.assigned):
            if s not in present:
                self.assigned.pop(s, None)
                self.assigned_manual.discard(s)
        for serial, model, sd, auto, state in rows:
            if serial not in self.assigned_manual:      # auto devices follow the live model match
                self.assigned[serial] = auto
            shown = self.assigned.get(serial, auto)
            tags = ("manual",) if serial in self.assigned_manual else ()
            self.dev_tree.insert("", "end", iid=serial, text=serial,
                                 values=(model, sd, shown, state), tags=tags)
        self.dev_tree.tag_configure("manual", foreground="#1a6f1a")
        self.log("refreshed: " + ", ".join(f"{r[0]} → {self.assigned.get(r[0], '?')}" for r in rows)
                 if rows else "refreshed: 0 device(s)")

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
            if self.dev_tree.exists(s):
                vals = list(self.dev_tree.item(s).get("values") or ["", "", "", ""])
                vals[2] = name
                self.dev_tree.item(s, values=vals, tags=("manual",))
        self.log(f"assigned profile '{name}' to: {', '.join(serials)}")

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
                                       root=self.profiles_root, log=self.log, profile_map=pm)
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
        if not messagebox.askyesno("CAS", f"Capture device {serial} into profile '{name}'?\n"
                                          "(the previous payload is kept as .prev for rollback)"):
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

    def new_profile(self):
        name = simpledialog.askstring("New profile", "New profile name (e.g. mangmi-airx-256):")
        if not name:
            return
        model = simpledialog.askstring("New profile",
                                       "model_match regex (matches ro.product.model), e.g. 'Air ?X':")
        d = P.pathlib.Path(self.profiles_root) / name
        if d.exists():
            messagebox.showerror("CAS", "A profile with that name already exists.")
            return
        d.mkdir(parents=True)
        (d / "profile.meta").write_text(
            f"model_match={model or ''}\nfrontend=\nnotes=\ncaptured=\n")
        (d / "manifest").write_text(f"# {name} (empty — capture a golden to populate)\n")
        self.log(f"created profile '{name}'. Select the golden device, then 'Capture / Update golden'.")
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
