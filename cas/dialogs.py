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
