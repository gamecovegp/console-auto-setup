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
from . import firmware as FW
from . import config
from . import theme as THEME


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


def human_size(nbytes):
    """Bytes -> '3.4 GB' / '512 MB' / '—' for 0."""
    n = float(nbytes or 0)
    if n <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_eta(secs):
    """Seconds -> '45s' / '4m 05s' / '1h 12m'."""
    secs = int(max(0, secs or 0))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def clamp_warmup(dwell_str, settle_str, cur_dwell, cur_settle):
    """Parse the two warm-up entry strings into (dwell, settle) floats. A blank/garbage/negative value
    falls back to the current value for that field (never crashes on operator typos). Pure — no Tk."""
    def _one(s, cur):
        try:
            v = float(str(s).strip())
        except (TypeError, ValueError):
            return float(cur)
        return v if v >= 0 else float(cur)
    return _one(dwell_str, cur_dwell), _one(settle_str, cur_settle)


class WarmupDialog:
    """Modal shown when ③ Warm up is about to run: pick how long the warm-up is for THIS run. Pre-filled
    with the current (last-used) values and PERSISTED on Run, so the choice sticks as the new default.
    `.result` is (dwell_s, settle_s), or None if cancelled."""

    def __init__(self, parent, cur_dwell, cur_settle):
        self.result = None
        self.win = win = tk.Toplevel(parent)
        win.title("Warm up — timing")
        win.transient(parent)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Warm-up timing", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frm, text="How long ③ Warm up spends opening each app so its emulator indexes before "
                            "Lock. Applies to this run and becomes the new default.",
                  style="Muted.TLabel", wraplength=380, justify="left").pack(anchor="w", pady=(2, 12))

        self.dwell_var = tk.StringVar(value=f"{cur_dwell:g}")
        self.settle_var = tk.StringVar(value=f"{cur_settle:g}")
        grid = ttk.Frame(frm)
        grid.pack(fill="x")
        ttk.Label(grid, text="Seconds each app stays open:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Spinbox(grid, from_=0, to=600, increment=1, width=7, textvariable=self.dwell_var) \
            .grid(row=0, column=1, sticky="e", padx=(10, 0))
        ttk.Label(grid, text="Final settle before Lock (seconds):").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Spinbox(grid, from_=0, to=600, increment=5, width=7, textvariable=self.settle_var) \
            .grid(row=1, column=1, sticky="e", padx=(10, 0))
        grid.columnconfigure(0, weight=1)

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=(16, 0))
        ttk.Button(bar, text="▶ Warm up", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 8))

        self._cur = (cur_dwell, cur_settle)
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Return>", lambda e: self._ok())
        _center(win, parent)
        win.grab_set()
        win.wait_window()

    def _ok(self):
        self.result = clamp_warmup(self.dwell_var.get(), self.settle_var.get(), *self._cur)
        self.win.destroy()


def _center(dlg, parent, dy=90):
    dlg.update_idletasks()
    try:
        x = parent.winfo_rootx() + (parent.winfo_width() - dlg.winfo_width()) // 2
        y = parent.winfo_rooty() + dy
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
    except tk.TclError:
        pass


def size_to_content(win, parent, min_w, min_h, pad=44):
    """Open `win` AT LEAST as tall/wide as its packed content actually requests, then clamp to the
    screen and place it near `parent`. Call AFTER every widget is packed.

    This is the real cure for 'the bottom buttons are cut off': the windows used to force a fixed
    geometry (e.g. 720x420) that was SHORTER than the content's natural height (~460px because a
    Treeview asks for ~10 rows). On a WM that honours the content's requested minimum rather than
    shrinking the tree, the extra height spills past the window edge and the button bar disappears.
    Measuring winfo_reqheight() and opening at least that tall makes it WM- and font-independent —
    no magic number to rebreak when a label wraps or the platform font changes."""
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w = min(max(min_w, win.winfo_reqwidth()), sw - 2 * pad)
    h = min(max(min_h, win.winfo_reqheight()), sh - 2 * pad)
    win.minsize(min_w, min(min_h, h))          # a floor; the bottom bar is pack-pinned for anything below it
    try:
        x = max(pad, min(parent.winfo_rootx() + 40, sw - w - pad))
        y = max(pad, min(parent.winfo_rooty() + 40, sh - h - pad))
    except (tk.TclError, AttributeError):
        x = y = pad
    win.geometry(f"{w}x{h}+{x}+{y}")
    return w, h                                 # the chosen size (returned so it's testable without a map)


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

        # Pin the action bar + the overwrite warning to the BOTTOM before the tree, so Run/Cancel are
        # never clipped no matter how short the window gets (see ProfilesWindow for the full rationale).
        bar = ttk.Frame(win, padding=(12, 10))
        bar.pack(fill="x", side="bottom")
        if on_new:
            ttk.Button(bar, text="＋ New profile…", command=self._new).pack(side="left")
        self.ok_btn = ttk.Button(bar, text=ok_text, style="Accent.TButton", command=self._ok)
        self.ok_btn.pack(side="right")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 8))

        self.warn_var = tk.StringVar(value="")
        ttk.Label(win, textvariable=self.warn_var, style="Warn.TLabel",
                  wraplength=480, justify="left").pack(anchor="w", padx=12, pady=(8, 0), side="bottom")

        self.tree = ttk.Treeview(win, columns=("model", "golden", "captured"),
                                 show="tree headings", selectmode="browse", height=9)
        self.tree.heading("#0", text="profile")
        self.tree.column("#0", width=190)
        for c, t, w in (("model", "model", 130), ("golden", "golden", 110), ("captured", "captured", 100)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        THEME.center_columns(self.tree)
        self.tree.pack(fill="both", expand=True, padx=12)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.bind("<Double-1>", lambda e: self._ok())

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
                vals[1] = human_size(nbytes)
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
            "device": fw.device or "—",
            "target": fw.flash_target or "—",
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

        ttk.Label(win, text="Profile library", style="Title.TLabel").pack(anchor="w", padx=12, pady=(12, 0))
        self.lib_var = tk.StringVar(value=f"Library: {app.profiles_root}")
        ttk.Label(win, textvariable=self.lib_var, style="Muted.TLabel").pack(anchor="w", padx=12)

        # Pin the button bar + the detail line to the BOTTOM *before* packing the tree, so they always
        # reserve their space and can never be clipped when the window opens at a fixed height smaller
        # than the tree's natural request (a Treeview asks for ~10 rows). The tree, packed last with
        # expand, shrinks to fill whatever is left. (Same load-bearing pack order as the main window.)
        bar = ttk.Frame(win, padding=(12, 10))
        bar.pack(fill="x", side="bottom")
        ttk.Button(bar, text="New…", command=self._new).pack(side="left")
        ttk.Button(bar, text="Set model…", command=self._set_model).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Delete…", command=self._delete).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Open folder", command=lambda: app._open_path(app.profiles_root)) \
            .pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")

        self.detail_var = tk.StringVar(value="Select a profile to see its golden.")
        ttk.Label(win, textvariable=self.detail_var, style="Muted.TLabel",
                  wraplength=680, justify="left").pack(anchor="w", padx=12, pady=(6, 0), side="bottom")

        self.tree = ttk.Treeview(win, columns=("model", "golden", "captured"),
                                 show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="profile"); self.tree.column("#0", width=200)
        for c, t, w in (("model", "model", 150), ("golden", "golden", 130), ("captured", "captured", 110)):
            self.tree.heading(c, text=t); self.tree.column(c, width=w)
        THEME.center_columns(self.tree)
        self.tree.pack(fill="both", expand=True, padx=12, pady=(8, 0))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        win.bind("<Escape>", lambda e: win.destroy())
        self.refresh()
        size_to_content(win, parent, 720, 460)     # open tall enough that the buttons are never clipped

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
            eta = (f" · ~{human_eta((b / 1048576.0) / mbps)} to download (avg {mbps:.0f} MB/s)"
                   if (mbps and b) else " · download time estimated after the first Download")
            self.win.after(0, lambda: self._set_detail(name, b, eta))
        threading.Thread(target=work, daemon=True).start()

    def _set_detail(self, name, nbytes, eta):
        try:
            self.detail_var.set(f"{name}: golden saved · {human_size(nbytes)}{eta}")
        except tk.TclError:
            pass                                          # window closed mid-size — nothing to update

    def _new(self):
        name = self.app.new_profile()
        if name:
            self.refresh(preselect=name)

    def _set_model(self):
        name = self._selected()
        if not name:
            messagebox.showinfo("CAS", "Select a profile row, then ‘Set model…’.")
            return
        self.app.set_profile_model(name)
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

        ttk.Label(win, text="Firmware library (device root firmware)",
                  style="Title.TLabel").pack(anchor="w", padx=12, pady=(12, 0))
        self.lib_var = tk.StringVar()
        ttk.Label(win, textvariable=self.lib_var, style="Muted.TLabel",
                  wraplength=720, justify="left").pack(anchor="w", padx=12)

        # Pin the button bar + the hint below the tree to the BOTTOM *before* packing the tree, so they
        # always reserve their space and can never be clipped when the window opens at a fixed height
        # smaller than the tree's natural request (~10 rows). The tree, packed last, shrinks to fill.
        bar = ttk.Frame(win, padding=(12, 10))
        bar.pack(fill="x", side="bottom")
        ttk.Button(bar, text="Add / update…", command=self._add).pack(side="left")
        ttk.Button(bar, text="Set as default kit", command=self._set_default_kit).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Open folder", command=self._open).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")

        ttk.Label(win, text="Assign firmware to a unit by right-clicking its row in the device list.  "
                            "★ = the build “(default kit)” flashes.",
                  style="Muted.TLabel").pack(anchor="w", padx=12, pady=(6, 0), side="bottom")

        self.tree = ttk.Treeview(win, columns=("version", "device", "target", "match"),
                                 show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="firmware id"); self.tree.column("#0", width=230)
        for c, t, w in (("version", "current", 90), ("device", "device", 130),
                        ("target", "flashes", 100), ("match", "serial prefix", 140)):
            self.tree.heading(c, text=t); self.tree.column(c, width=w)
        THEME.center_columns(self.tree)
        self.tree.pack(fill="both", expand=True, padx=12, pady=(8, 0))

        win.bind("<Escape>", lambda e: win.destroy())
        self.refresh()
        size_to_content(win, parent, 760, 460)     # open tall enough that the buttons are never clipped

    def _root(self):
        try:
            return FW.firmware_root()
        except Exception:
            return None

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        root = self._root()
        rows = firmware_rows(root) if root else []
        dk = config.default_kit_firmware()                     # the build “(default kit)” maps to, if any
        for r in rows:
            star = "★  " if r["id"] == dk else ""              # mark the designated default-kit build
            self.tree.insert("", "end", iid=r["id"], text=star + r["id"],
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
            base = (f"Library: {configured}   ✗ unreachable (library drive unmounted?) — "
                    f"falling back to {root}")
        elif not _isdir(root):
            base = f"Library: {root}   ✗ not reachable (connect the library drive?)"
        elif not rows:
            base = f"Library: {root}   ✓ (no firmware yet — use “Add / update…”)"
        else:
            base = f"Library: {root}   ✓ ({len(rows)} firmware)"
        # Surface what “(default kit)” resolves to — the whole point of designating one.
        if dk and any(r["id"] == dk for r in rows):
            base += f"   ·   ★ default kit → {dk}"
        elif dk:
            base += f"   ·   ⚠ default kit “{dk}” is not in this library"
        else:
            base += "   ·   no default kit set (‘(default kit)’ falls back to the bundled odin2 path)"
        self.lib_var.set(base)

    def _set_default_kit(self):
        """Designate the selected build as the '(default kit)' — the init_boot Root flashes when a
        device is pinned to '(default kit)'. Re-selecting the current one clears it."""
        sel = self.tree.selection()
        fid = sel[0] if sel else None
        if not fid:
            messagebox.showinfo("CAS", "Select a firmware row, then ‘Set as default kit’.")
            return
        if fid == config.default_kit_firmware():
            config.set_default_kit_firmware(None)              # toggle off
            self.app.log(f"default kit cleared (‘(default kit)’ falls back to the bundled path).")
        else:
            config.set_default_kit_firmware(fid)
            self.app.log(f"default kit → {fid}: devices pinned to ‘(default kit)’ now flash its init_boot.")
        self.refresh()
        self.app.refresh_devices()                             # re-resolve so any '(default kit)' rows update

    def _add(self):
        self.app._add_firmware(on_done=self._on_ingest_done)   # threaded ingest; refresh when it actually lands

    def _on_ingest_done(self):
        try:
            self.refresh()
        except tk.TclError:
            pass                                      # window closed mid-ingest — nothing to refresh

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
