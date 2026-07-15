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

# Lock the coupling: gui._populate_devices does THEME.LIGHT[key] for every STATE_COLORS value, on the UI
# thread, mid device-list refresh. A value that isn't a LIGHT key is a KeyError right there — which empties
# the whole device list instead of failing loudly here, at import time. A bare `assert` would be stripped
# under `python -O`, so this is an explicit raise — the invariant must hold even in an optimized build.
_bad_state_colors = sorted({v for v in STATE_COLORS.values() if v not in LIGHT})
if _bad_state_colors:
    raise ValueError(f"STATE_COLORS value(s) not in LIGHT: {_bad_state_colors}")

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
        base.configure(family=ui)          # switch the family ONLY — keep the platform's own default size
    family = base.cget("family")
    # Derive the type scale from that platform default (10pt Linux, 9pt Windows, 13pt macOS) instead of
    # hard-coding 10, which shrank mac text and enlarged Windows. `size` may be negative on some builds
    # (Tk reads a negative size as pixels) — preserve the sign so the +3/-1 offsets scale in either unit.
    sz = base.cget("size") or 10
    unit = -1 if sz < 0 else 1
    pts = abs(sz)
    fonts = {
        "title":   tkfont.Font(root=root, family=family, size=unit * (pts + 3), weight="bold"),
        "body":    base,
        "caption": tkfont.Font(root=root, family=family, size=unit * max(1, pts - 1)),
        "mono":    tkfont.Font(root=root, family=(mono if mono != "TkFixedFont" else family),
                               size=unit * max(1, pts - 1)),
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
