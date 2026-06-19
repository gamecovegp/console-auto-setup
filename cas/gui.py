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
import threading
import datetime
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog, filedialog

from . import APPDIR, BUNDLE, __version__
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
        win.title("CAS — Console Auto Setup")
        win.geometry("1000x720")
        win.minsize(820, 470)       # keep the action bar reachable even when shrunk
        self._build_menu()
        self._build()
        self._poll_log()
        self.refresh_profiles()
        self.refresh_devices()

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
        helpm.add_command(label="About CAS", command=self._about)
        bar.add_cascade(label="Help", menu=helpm)

        self.win.config(menu=bar)
        self.win.bind_all("<Control-r>", lambda e: self.refresh_devices())
        self.win.bind_all("<Control-q>", lambda e: self.win.destroy())

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
                                     show="tree headings", height=7)
        self.dev_tree.heading("#0", text="serial")
        for c, t, w in (("model", "model", 120), ("sd", "SD card", 135),
                        ("profile", "auto profile", 110), ("state", "state", 70)):
            self.dev_tree.heading(c, text=t)
            self.dev_tree.column(c, width=w)
        self.dev_tree.column("#0", width=120)
        self.dev_tree.pack(fill="both", expand=True)
        _tip(ttk.Button(devf, text="Refresh devices", command=self.refresh_devices),
             "Re-scan for plugged-in devices and re-match each one to its profile by model.") \
            .pack(anchor="w", pady=(6, 0))

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

        # Action area: an "apply to ALL connected" toggle (+ a "force selected profile" sub-toggle) and the
        # workflow buttons in order: Root -> Save a master -> Download -> Lock. Root, Download and Lock honor
        # the toggle (run on every connected device IN PARALLEL — each AUTO-MATCHED to its own profile by
        # model, or the forced profile if that sub-toggle is on); only Save is always single-device.
        # Footer pinned to the window BOTTOM so the action buttons stay visible even on a short window —
        # the devices/profile/log areas above shrink instead of pushing the buttons off-screen (the cause
        # of the "buttons have no text" clip on small screens).
        footer = ttk.Frame(self.win)
        footer.pack(side="bottom", fill="x")
        act = ttk.Frame(footer, padding=(8, 0))
        act.pack(side="top", fill="x")
        self.batch_var = tk.BooleanVar(value=False)
        _tip(ttk.Checkbutton(act, text="Apply to ALL connected devices  (Root, Download + Lock, in parallel)",
                             variable=self.batch_var, command=self._on_batch_toggle),
             "OFF: the action runs on the ONE device selected in the list.\n"
             "ON: Root, Download and Lock run on EVERY connected device IN PARALLEL (all at once). By "
             "default each device is AUTO-MATCHED to its own profile by model (the golden is never sealed); "
             "tick 'Force selected profile' below to override.") \
            .pack(anchor="w", pady=(2, 0))
        self.force_profile_var = tk.BooleanVar(value=False)
        _tip(ttk.Checkbutton(act, text="      ↳ Force the SELECTED profile on every device  "
                                       "(default: auto-match each by model)",
                             variable=self.force_profile_var),
             "Only affects 'Apply to ALL'.\n"
             "OFF (default): each connected device gets the profile whose model_match fits it; a device "
             "with no matching profile is skipped.\n"
             "ON: the profile picked in the dropdown is applied to EVERY device. (Root/Lock still skip a "
             "model mismatch to avoid a wrong-init_boot brick; Download applies regardless.)") \
            .pack(anchor="w", pady=(0, 2))
        row2 = ttk.Frame(act)
        row2.pack(fill="x")
        self.btns = []
        for text, cmd, tip in (
            ("⓪ Root", self.root_device,
             "Root a FRESH unit with Magisk, sourced entirely from the PC: flashes the profile's "
             "Magisk-patched init_boot via fastboot, then installs the Magisk app FROM THE PC (not the "
             "SD). Bootloader must be UNLOCKED; the unit reboots a couple of times. With 'Apply to ALL' "
             "ticked, roots EVERY connected device IN PARALLEL. (Inverse of 'Lock'.)"),
            ("① Save device → profile", self.capture_update,
             "SAVE what's on the selected device INTO a profile on this PC (the master 'golden'). Always "
             "ONE device. The previous version is kept as .prev for rollback. Direction: device → PC."),
            ("② Download", self.provision_selected,
             "DOWNLOAD a profile onto the device — installs every ticked app plus its saves/BIOS/keys, "
             "settings, folder permissions, and homescreen layout (replaces current app data). "
             "Direction: PC → device. With 'Apply to ALL' ticked, downloads the SELECTED profile to "
             "EVERY connected device IN PARALLEL. (Formerly 'Provision'.)"),
            ("③ Lock for shipping", self.seal_device,
             "Final step on a VERIFIED unit: HIDES Developer options, removes root (Magisk), and disables "
             "USB debugging so it's retail-ready (adb disconnects). With 'Apply to ALL' ticked, seals every "
             "connected device IN PARALLEL (the golden + mismatched models are skipped). (Inverse of 'Root'.)"),
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
        # behavior flags (@settings/@hardening/@grants/@homescreen) — honored by restore.sh
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
                model, sd, prof = "", "", ""
                if state == "device":
                    a = Adb(serial=serial, adb=self.adb_bin)
                    model = a.getprop("ro.product.model")
                    m = P.match_profile(model, self.profiles_root)
                    prof = m.name if m else "(no match)"
                    try:
                        sd = a.sd_info()        # SD serial + size (or 'no SD') — catches wrong/missing cards
                    except Exception:
                        sd = "?"
                rows.append((serial, model, sd, prof, state))
            self.win.after(0, lambda: [self.dev_tree.insert("", "end", iid=r[0], text=r[0],
                                       values=(r[1], r[2], r[3], r[4])) for r in rows])
            self.log("refreshed: " + ", ".join(f"{r[0]} [SD {r[2] or 'n/a'}]" for r in rows) if rows
                     else "refreshed: 0 device(s)")
        threading.Thread(target=work, daemon=True).start()

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
            self.status_var.set("Batch mode ON — Root, Download & Lock run on ALL connected devices IN "
                                "PARALLEL, each AUTO-MATCHED to its own profile (tick 'Force selected "
                                "profile' to override). Only Save stays single-device.")
        else:
            self.status_var.set("Single-device mode — actions run on the selected device.")

    def _selected_profile(self):
        name = self.prof_var.get()
        return P.Profile(P.pathlib.Path(self.profiles_root) / name) if name else None

    def _batch_target(self):
        """For 'Apply to ALL': pick the profile to apply. Returns (profile_or_None, description), or None
        to ABORT. Default = AUTO-MATCH each device to its own profile by model (profile=None → the backend
        matches on ro.product.model). If 'Force selected profile' is ticked, the dropdown profile is forced
        onto every device."""
        if self.force_profile_var.get():
            prof = self._selected_profile()
            if not prof:
                messagebox.showinfo("CAS", "'Force selected profile' is ticked but no profile is selected.\n"
                                           "Pick a profile, or untick it to auto-match each device by model.")
                return None
            return (prof, f"the FORCED profile '{prof.name}'")
        return (None, "each device's auto-matched profile (by model)")

    def provision_selected(self):
        if self.batch_var.get():
            return self.provision_all()
        serial = self._selected_serial()
        if not serial:
            messagebox.showinfo("CAS", "Select a device in the list first.")
            return
        name = self.prof_var.get()
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name) if name else None
        if not prof:
            messagebox.showinfo("CAS", "Pick a profile (or it will auto-match).")
            return
        def work():
            ok = PV.provision(Adb(serial=serial, adb=self.adb_bin), prof, log=self.log)
            if not ok:   # arm retry of the same device
                self._retry_ctx = (f"Download to {serial} failed. Try again?", self.provision_selected)
            return ok
        self._run_bg(work, label=f"Downloading '{name}' to {serial}")

    def provision_all(self, devices=None):
        tgt = self._batch_target()
        if tgt is None:
            return
        prof, desc = tgt
        if devices is None:   # first run (not a retry) -> confirm
            if not messagebox.askyesno(
                    "CAS", f"Download to ALL connected devices (in parallel) using {desc}?"):
                return
        def work():
            devs = devices if devices is not None else list_devices(adb=self.adb_bin)
            res = PV.provision_all(lambda s: Adb(serial=s, adb=self.adb_bin), devs,
                                   root=self.profiles_root, log=self.log, profile=prof)
            self.win.after(0, self.refresh_devices)
            failed = [s for s, v in res.items() if v[0] in ("fail", "error")]
            if failed:   # arm retry: re-run JUST the failed devices (succeeded ones left alone)
                self._retry_ctx = (
                    f"{len(failed)} device(s) failed Download:\n  {', '.join(failed)}\n\n"
                    "Retry just those? (devices that already succeeded are left as-is.)",
                    lambda fs=failed: self.provision_all(devices=[(s, "device") for s in fs]))
            return res
        self._run_bg(work, label=f"Downloading to all connected ({desc})"
                                 f"{' (retry)' if devices is not None else ''}")

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

    def _root_all(self, devices=None):
        tgt = self._batch_target()
        if tgt is None:
            return
        prof, desc = tgt
        if devices is None:   # first run (not a retry) -> confirm
            if not messagebox.askyesno(
                    "CAS — Root ALL connected",
                    f"Root EVERY connected device using {desc} (Magisk from the PC)?\n\n"
                    "Each device's Magisk-patched init_boot is flashed to it; a device whose model doesn't "
                    "match its profile is skipped (safety). Bootloaders must be UNLOCKED. Devices reboot a "
                    "couple of times. They run IN PARALLEL. Proceed?"):
                return
        def work():
            devs = devices if devices is not None else list_devices(adb=self.adb_bin)
            res = PV.root_all(lambda s: Adb(serial=s, adb=self.adb_bin),
                              lambda s: Fastboot(serial=s, fastboot=self.fb_bin),
                              devs, profiles_root=self.profiles_root,
                              appdir=APPDIR, log=self.log, profile=prof)
            self.win.after(0, self.refresh_devices)
            failed = [s for s, v in res.items() if v[0] in ("fail", "error")]
            if failed:   # arm retry: re-run JUST the failed devices
                self._retry_ctx = (
                    f"{len(failed)} device(s) failed Root:\n  {', '.join(failed)}\n\nRetry just those?",
                    lambda fs=failed: self._root_all(devices=[(s, "device") for s in fs]))
            return res
        self._run_bg(work, label=f"Rooting all connected ({desc})"
                                 f"{' (retry)' if devices is not None else ''}")

    def root_device(self):
        if self.batch_var.get():
            return self._root_all()
        serial = self._selected_serial()
        if not serial:
            messagebox.showinfo("CAS", "Select the device to root first.")
            return
        name = self.prof_var.get()
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name) if name else None
        patched_rel = prof.meta.get("patched_init_boot") if prof else None
        if not patched_rel:
            messagebox.showerror("CAS", "This profile has no 'patched_init_boot' set — needed to root.")
            return
        patched = APPDIR / patched_rel
        magisk_rel = prof.meta.get("magisk_apk")
        magisk = (APPDIR / magisk_rel) if magisk_rel else None
        if not patched.exists():
            messagebox.showerror("CAS", f"The profile's patched init_boot is missing on the PC:\n{patched}")
            return
        # If the device differs from the profile, DON'T block — warn and ask. Same-chipset siblings can
        # often share one image; the user decides, and force=True lets root() proceed past its guard.
        vals = self.dev_tree.item(serial).get("values") or []
        model = str(vals[0]) if vals else ""
        mm = prof.meta.get("model_match")
        force = False
        if model and mm and not re.search(mm, model):
            if not messagebox.askyesno(
                    "CAS — different device than the profile",
                    f"⚠ This device is '{model}', but the '{name}' profile is for '{mm}'.\n\n"
                    f"Rooting flashes this profile's Magisk-patched init_boot, which was built for a "
                    f"DIFFERENT device. If they're the same chipset it will likely work; if NOT compatible "
                    f"the unit can bootloop — recoverable by re-flashing its own stock init_boot.\n\n"
                    f"Proceed with the '{name}' setup anyway?"):
                return
            force = True
        elif not messagebox.askyesno(
                "CAS — Root device",
                "Root this device with Magisk (sourced from the PC)?\n\n"
                "  • flashes the profile's Magisk-patched init_boot via fastboot\n"
                "  • installs the Magisk app FROM THE PC (not the SD)\n\n"
                "The bootloader must be UNLOCKED. The device reboots a couple of times. Proceed?"):
            return
        self._run_bg(lambda: PV.root(
            Adb(serial=serial, adb=self.adb_bin),
            Fastboot(serial=serial, fastboot=self.fb_bin),
            patched, magisk_apk=magisk, log=self.log,
            model_match=prof.meta.get("model_match"), force=force),
            label=f"Rooting {serial}{' (FORCED — different device)' if force else ''}")

    def seal_device(self):
        if self.batch_var.get():
            return self._seal_all()
        serial = self._selected_serial()
        if not serial:
            messagebox.showinfo("CAS", "Select the provisioned device first.")
            return
        name = self.prof_var.get()
        prof = P.Profile(P.pathlib.Path(self.profiles_root) / name) if name else None
        stock_rel = prof.meta.get("stock_init_boot") if prof else None
        if not stock_rel:
            messagebox.showerror("CAS", "This profile has no 'stock_init_boot' set — needed to un-root.")
            return
        stock = APPDIR / stock_rel
        if not stock.exists():
            messagebox.showerror("CAS", f"The profile's stock init_boot is missing on the PC:\n{stock}")
            return
        # Same as Root: if the device differs from the profile, warn + ask instead of silently refusing.
        vals = self.dev_tree.item(serial).get("values") or []
        model = str(vals[0]) if vals else ""
        mm = prof.meta.get("model_match")
        force = False
        if model and mm and not re.search(mm, model):
            if not messagebox.askyesno(
                    "CAS — different device than the profile",
                    f"⚠ This device is '{model}', but the '{name}' profile is for '{mm}'.\n\n"
                    f"Sealing flashes this profile's STOCK init_boot (to un-root), which was built for a "
                    f"DIFFERENT device. Same chipset will likely work; if NOT compatible the unit can "
                    f"bootloop — recoverable by re-flashing its own stock init_boot.\n\n"
                    f"Proceed with the '{name}' stock image anyway?"):
                return
            force = True
        elif not messagebox.askyesno(
                "CAS — Seal for retail",
                "Seal this unit for shipping?\n\n"
                "Assumes you have VERIFIED games boot. This will:\n"
                "  • disable Developer options\n"
                "  • remove Magisk (UN-ROOT via stock init_boot)\n"
                "  • disable USB debugging (adb will disconnect)\n\n"
                "To re-provision later you'd re-flash the patched init_boot. Proceed?"):
            return
        self._run_bg(lambda: PV.seal(
            Adb(serial=serial, adb=self.adb_bin),
            Fastboot(serial=serial, fastboot=self.fb_bin),
            stock, log=self.log, model_match=prof.meta.get("model_match"), force=force),
            label=f"Locking {serial} for shipping{' (FORCED — different device)' if force else ''}")

    def _seal_all(self, devices=None):
        tgt = self._batch_target()
        if tgt is None:
            return
        prof, desc = tgt
        if devices is None:   # first run (not a retry) -> confirm
            if not messagebox.askyesno(
                    "CAS — Lock ALL connected",
                    f"Seal EVERY connected device for shipping using {desc}?\n\n"
                    "Assumes you've VERIFIED each unit. For every device this hides Developer options, "
                    "removes Magisk (UN-ROOT via stock init_boot), and disables USB debugging. The golden "
                    "and any model that doesn't match its profile are skipped. They run IN PARALLEL. Proceed?"):
                return
        def work():
            devs = devices if devices is not None else list_devices(adb=self.adb_bin)
            res = PV.seal_all(lambda s: Adb(serial=s, adb=self.adb_bin),
                              lambda s: Fastboot(serial=s, fastboot=self.fb_bin),
                              devs, profiles_root=self.profiles_root,
                              appdir=APPDIR, log=self.log, profile=prof)
            self.win.after(0, self.refresh_devices)
            failed = [s for s, v in res.items() if v[0] in ("fail", "error")]
            if failed:   # arm retry: re-run JUST the failed devices
                self._retry_ctx = (
                    f"{len(failed)} device(s) failed Lock:\n  {', '.join(failed)}\n\nRetry just those?",
                    lambda fs=failed: self._seal_all(devices=[(s, "device") for s in fs]))
            return res
        self._run_bg(work, label=f"Locking all connected ({desc})"
                                 f"{' (retry)' if devices is not None else ''}")

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
