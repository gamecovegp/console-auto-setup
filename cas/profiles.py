"""Profile library: list / match-by-model / manifest parse+save / archive (soft-delete).

A profile is a directory under `profiles/<name>/` with:
  profile.meta            key=value (model_match, frontend, notes, captured)
  manifest                app names (one per line) + "@flag value" + "#" comments
  golden_root_payload/    the captured payload (per-app modules + internal_*.tar + grants + settings)
"""
import re
import pathlib
import shutil

# Emulator/frontend packages = the GAMING payload, auto-checked in the Save (capture) list.
# Keep in sync with provision/root/lib-root.sh PKGS.
EMULATOR_PKGS = {
    "dev.eden.eden_emulator", "com.retroarch.aarch64", "org.dolphinemu.dolphinemu",
    "com.flycast.emulator", "com.github.stenzek.duckstation", "xyz.aethersx2.android",
    "me.magnum.melonds.nightly", "org.citra.emu", "org.ppsspp.ppsspp",
    "org.mupen64plusae.v3.fzurita", "org.es_de.frontend", "gamehub.lite",
}

# pkg -> shared internal-storage dir it owns (mirror of lib-root.sh:internal_for). Restored only if the
# app is in the manifest.
INTERNAL_FOR = {
    "org.es_de.frontend": "ES-DE",
    "org.citra.emu": "citra-emu",
    "com.retroarch.aarch64": "RetroArch",
}


def internal_for(pkg):
    return INTERNAL_FOR.get(pkg)


def _dir_bytes(path):
    """Total size in bytes of all files under `path` (0 if missing). Best-effort — ignores stat errors."""
    total = 0
    try:
        for f in pathlib.Path(path).rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _read_text(path):
    """Read text tolerant of non-UTF-8 bytes. NAS profiles authored on Windows can carry cp1252 bytes
    (e.g. an em-dash 0x97) in a manifest/meta line that strict UTF-8 decoding would crash on."""
    return pathlib.Path(path).read_text(encoding="utf-8", errors="replace")


def _read_meta(path):
    meta = {}
    p = pathlib.Path(path)
    if p.exists():
        for line in _read_text(p).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                meta[k.strip()] = v.strip()
    return meta


def set_meta_key(path, key, value):
    """Set key=value in a profile.meta, updating the line in place if present (order + comments preserved)
    or appending it. Creates the file if missing. Used by the GUI's 'Root images' picker to record
    stock_init_boot / magisk_apk without clobbering hand-written keys."""
    p = pathlib.Path(path)
    lines = _read_text(p).splitlines() if p.exists() else []
    out, replaced = [], False
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    p.write_text("\n".join(out) + "\n")


def resolve_asset(prof, appdir, rel):
    """Resolve a profile.meta asset path (patched_init_boot / stock_init_boot / magisk_apk). Prefer a file
    sitting INSIDE the profile dir — that's where per-unit captured images live — else fall back to
    appdir-relative (the shared firmware library / repo-relative paths existing profiles use). Absolute
    paths pass through unchanged."""
    p = pathlib.Path(rel)
    if p.is_absolute():
        return p
    local = prof.path / rel
    if local.exists():
        return local
    return pathlib.Path(appdir) / rel


def manifest_pkgs(manifest_path):
    """App names from a manifest (comments + @flag lines stripped)."""
    p = pathlib.Path(manifest_path)
    if not p.exists():
        return []
    out = []
    for line in _read_text(p).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("@"):
            continue
        out.append(line.split()[0])
    return out


def manifest_flags(manifest_path):
    """{flag: value} from @flag lines (value defaults to 'on' if omitted)."""
    p = pathlib.Path(manifest_path)
    flags = {}
    if not p.exists():
        return flags
    for line in _read_text(p).splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("@"):
            parts = line[1:].split()
            if parts:
                flags[parts[0]] = parts[1] if len(parts) > 1 else "on"
    return flags


def manifest_axes(manifest_path):
    """{pkg: (apk_bool, config_bool)} from manifest app lines. A bare line (no tokens)
    means BOTH axes (back-compat). Tokens 'apk' and/or 'config' narrow it."""
    p = pathlib.Path(manifest_path)
    axes = {}
    if not p.exists():
        return axes
    for line in _read_text(p).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("@"):
            continue
        parts = line.split()
        pkg, toks = parts[0], set(parts[1:])
        if toks:
            axes[pkg] = ("apk" in toks, "config" in toks)
        else:
            axes[pkg] = (True, True)
    return axes


def save_manifest(manifest_path, pkgs, flags, header="# manifest", axes=None):
    def _line(pkg):
        if not axes or pkg not in axes:
            return pkg                       # bare = both axes (back-compat)
        apk, cfg = axes[pkg]
        toks = ([] if (apk and cfg) else (["apk"] if apk else []) + (["config"] if cfg else []))
        return pkg if not toks else f"{pkg} {' '.join(toks)}"
    lines = [header]
    lines += [_line(p) for p in pkgs]
    lines += [f"@{k} {v}" for k, v in flags.items()]
    pathlib.Path(manifest_path).write_text("\n".join(lines) + "\n")


class Profile:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.name = self.path.name
        self.meta = _read_meta(self.path / "profile.meta")
        self.manifest_path = self.path / "manifest"
        self.payload = self.path / "golden_root_payload"

    def pkgs(self):
        return manifest_pkgs(self.manifest_path)

    def flags(self):
        return manifest_flags(self.manifest_path)

    def axes(self):
        """{pkg: (apk_bool, config_bool)} per-app capture selection from the manifest (bare line = both)."""
        return manifest_axes(self.manifest_path)

    @property
    def capture_manifest_path(self):
        return self.path / "capture-manifest"

    def capture_pkgs(self):
        return manifest_pkgs(self.capture_manifest_path)

    def capture_axes(self):
        return manifest_axes(self.capture_manifest_path)

    def capture_flags(self):
        return manifest_flags(self.capture_manifest_path)

    def all_pkgs(self):
        """Every selectable app: the captured set (pkglist.txt) plus the default launcher (a system app
        excluded by user_pkgs) when known, so it can be ticked for config. The full toggle set for the UI."""
        pl = self.payload / "pkglist.txt"
        pkgs = ([l.strip() for l in _read_text(pl).splitlines() if l.strip()]
                if pl.exists() else self.pkgs())
        lp = self.meta.get("launcher_pkg") or _read_meta(self.payload / "homescreen" / "meta").get("launcher_pkg")
        if lp and lp not in pkgs:
            pkgs.append(lp)
        return pkgs

    def has_golden(self):
        """True if a golden has been captured into this profile (payload + its global.meta both exist)."""
        return (self.payload / "global.meta").exists()

    def golden_size(self):
        """Total bytes of the captured golden payload (0 if none) — used for the storage + download ETA."""
        return _dir_bytes(self.payload) if self.payload.exists() else 0

    def __repr__(self):
        return f"<Profile {self.name} frontend={self.meta.get('frontend')}>"


def list_profiles(root="profiles"):
    root = pathlib.Path(root)
    if not root.exists():
        return []
    return [Profile(p) for p in sorted(root.iterdir())
            if p.is_dir() and (p / "profile.meta").exists()]


# --- model / SD-size matching -------------------------------------------------------------------
_CAP_MIN_GB = 32        # a number >= this (in a profile name or an SD size) is a STORAGE CAPACITY, not a
#                         model version — so "Pocket 6" keeps 6 as a model word while "…-512" is a tier.


def _toks(s):
    """Lowercase alphanumeric tokens: 'Retroid Pocket 6' -> ['retroid','pocket','6']."""
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def _leading_int(tok):
    m = re.match(r"(\d+)", tok)
    return int(m.group(1)) if m else None


def _is_capacity(tok):
    n = _leading_int(tok)
    return n is not None and n >= _CAP_MIN_GB


def parse_sd_gb(sd_desc):
    """GB from an sd_info() string like '9C33-6BBD · 477G', '238G', or '1T'. None if no size is present."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*([TG])", (sd_desc or ""), re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    return val * 1024 if m.group(2).upper() == "T" else val


def match_profile(model, root="profiles", sd_gb=None):
    """Pick the profile for a device by MODEL, using the SD-card size to break ties between per-capacity
    tiers — no hand-written regex required.
      1. If any profile sets an explicit `model_match`, those END-ANCHORED regexes win (back-compat).
      2. Else match by NAME similarity: a profile is a candidate when ALL its non-capacity name words
         appear in the device model (profile 'retroid-pocket-6-512' -> {retroid,pocket,6} ⊆ 'Retroid
         Pocket 6'); the most specific (most model words matched) win.
      3. If >1 candidate remains (e.g. '…-512' vs '…-256'), pick the one whose capacity is NEAREST the
         device's SD size. Still tied, or no size to compare -> None (operator assigns manually).
    Returns the Profile, or None."""
    model = (model or "").strip()
    if not model:
        return None
    profs = list_profiles(root)
    # 1) explicit regex profiles win (END-ANCHORED, case-sensitive — unchanged behaviour)
    cands = [p for p in profs if p.meta.get("model_match")
             and re.search(f"(?:{p.meta['model_match']})$", model)]
    # 2) default: name-similarity (the most specific model coverage wins)
    if not cands:
        mt = set(_toks(model))
        if mt:
            sims = []
            for p in profs:
                pmodel = {t for t in (_toks(p.name) + _toks(p.meta.get("model_match", "")))
                          if not _is_capacity(t)}
                if pmodel and pmodel <= mt and len(mt & pmodel) / len(mt) >= 0.6:
                    sims.append((len(mt & pmodel), p))
            if sims:
                top = max(c for c, _ in sims)
                cands = [p for c, p in sims if c == top]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    # 3) several tiers match -> the SD card's size chooses the closest one
    if sd_gb:
        scored = []
        for p in cands:
            caps = [_leading_int(t) for t in _toks(p.name) if _is_capacity(t)]
            if caps:
                scored.append((min(abs(c - sd_gb) for c in caps), p))
        scored.sort(key=lambda x: x[0])
        if scored and (len(scored) == 1 or scored[0][0] != scored[1][0]):
            return scored[0][1]
    return None                          # ambiguous -> operator assigns (double-click / Assign button)


def default_capture_selection(device_apps, game_launcher=None, home_launcher=None):
    """The default Save-list check state: {pkg: (apk_on, config_on)}. Emulators (EMULATOR_PKGS) -> both axes;
    the game/HOME launcher -> config-only (APK is system firmware); every other device app -> off."""
    sel = {}
    for pkg in device_apps:
        on = pkg in EMULATOR_PKGS
        sel[pkg] = (on, on)
    for lp in (game_launcher, home_launcher):
        if lp:
            sel[lp] = (False, lp == game_launcher)   # game launcher config-on by default; HOME off
    return sel


def initial_capture_selection(device_apps, saved_axes, saved_flags, game_launcher=None, home_launcher=None):
    """The Save-list initial check state: default_capture_selection, overlaid by a saved capture-manifest's
    package axes, then the launcher rows seeded from the saved @gamelauncher/@homescreen flags (launcher
    selection is persisted as flags, not package lines). Pure — no I/O."""
    sel = default_capture_selection(device_apps, game_launcher, home_launcher)
    for pkg, axes_pair in (saved_axes or {}).items():
        sel[pkg] = axes_pair
    if game_launcher and game_launcher in sel:
        sel[game_launcher] = (False, (saved_flags or {}).get("gamelauncher", "on") == "on")
    if home_launcher and home_launcher in sel:
        sel[home_launcher] = (False, (saved_flags or {}).get("homescreen", "off") == "on")
    return sel


def archive_profile(profile, stamp, archive_root=None):
    """Soft-delete: MOVE the profile dir to profiles/_archive/<name>_<stamp>. Never rm. Returns dest."""
    src = pathlib.Path(profile.path)
    if archive_root is None:
        archive_root = src.parent / "_archive"
    dst = pathlib.Path(archive_root) / f"{profile.name}_{stamp}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst
