"""Profile library: list / match-by-model / manifest parse+save / archive (soft-delete).

A profile is a directory under `profiles/<name>/` with:
  profile.meta            key=value (model_match, frontend, notes, captured)
  manifest                app names (one per line) + "@flag value" + "#" comments
  golden_root_payload/    the captured payload (per-app modules + internal_*.tar + grants + settings)
"""
import re
import struct
import zipfile
import pathlib
import shutil


def _axml_string_pool(data, pos):
    """Decode a binary-XML (AXML) string pool chunk at `pos` -> list of strings. Handles UTF-8 and UTF-16."""
    u32 = lambda o: struct.unpack_from("<I", data, o)[0]
    count = u32(pos + 8); flags = u32(pos + 16); strings_start = u32(pos + 20)
    utf8 = bool(flags & 0x100)

    def _len(off):                                   # var-length string length prefix
        if utf8:
            n = data[off]; off += 1
            if n & 0x80:
                n = ((n & 0x7f) << 8) | data[off]; off += 1
            return n, off
        n = struct.unpack_from("<H", data, off)[0]; off += 2
        if n & 0x8000:
            n = ((n & 0x7fff) << 16) | struct.unpack_from("<H", data, off)[0]; off += 2
        return n, off

    out = []
    for i in range(count):
        sp = pos + strings_start + u32(pos + 28 + i * 4)
        if utf8:
            _, sp = _len(sp)                         # char count (skip)
            blen, sp = _len(sp)                      # byte count
            out.append(data[sp:sp + blen].decode("utf-8", "replace"))
        else:
            n, sp = _len(sp)
            out.append(data[sp:sp + n * 2].decode("utf-16-le", "replace"))
    return out


def apk_package_id(apk_path):
    """The `<manifest package="…">` id read straight from an APK's binary AndroidManifest.xml — a pure
    Python AXML parse, NO aapt/external tools (so Add-APK can auto-fill the package id from the chosen
    file). Returns the package string, or None on any problem (not an APK, no manifest, parse failure)."""
    try:
        with zipfile.ZipFile(apk_path) as z:
            data = z.read("AndroidManifest.xml")
    except Exception:
        return None
    try:
        u16 = lambda o: struct.unpack_from("<H", data, o)[0]
        u32 = lambda o: struct.unpack_from("<I", data, o)[0]
        pos, strings = 8, None                       # skip the 8-byte file header
        while pos + 8 <= len(data):
            ctype, chsize = u16(pos), u32(pos + 4)
            if chsize <= 0:
                break
            if ctype == 0x0001:                      # RES_STRING_POOL_TYPE
                strings = _axml_string_pool(data, pos)
            elif ctype == 0x0102 and strings is not None:   # RES_XML_START_ELEMENT_TYPE
                name = u32(pos + 20)
                if name < len(strings) and strings[name] == "manifest":
                    astart = u16(pos + 24); asize = u16(pos + 26) or 20; acount = u16(pos + 28)
                    ap = pos + 16 + astart
                    for _ in range(acount):
                        aname, raw, tdata = u32(ap + 4), u32(ap + 8), u32(ap + 16)
                        if aname < len(strings) and strings[aname] == "package":
                            idx = raw if raw != 0xFFFFFFFF else tdata
                            return strings[idx] if idx < len(strings) else None
                        ap += asize
                    return None
            pos += chsize
    except Exception:
        return None
    return None

# Emulator/frontend packages = the GAMING payload, auto-checked in the Save (capture) list.
# Keep in sync with provision/root/lib-root.sh PKGS.
EMULATOR_PKGS = {
    "dev.eden.eden_emulator", "com.retroarch.aarch64", "org.dolphinemu.dolphinemu",
    "com.flycast.emulator", "com.github.stenzek.duckstation", "xyz.aethersx2.android",
    "xyz.aethersx2.tturnip", "me.magnum.melonds.nightly", "org.citra.emu", "org.ppsspp.ppsspp",
    "org.mupen64plusae.v3.fzurita", "org.es_de.frontend", "gamehub.lite",
}

# Apps whose APK is provided EXTERNALLY (sideloaded / a custom build), so the golden carries only their
# config/BIOS — never the APK. Save defaults these to config-only; on Download CAS won't install the APK
# (you sideload it). PS2 ships under either id depending on the unit: xyz.aethersx2.android (AetherSX2, or
# GameCove's NetherSX2 repackaged under that id) and xyz.aethersx2.tturnip (NetherSX2-Turnip).
CONFIG_ONLY_PKGS = {"xyz.aethersx2.android", "xyz.aethersx2.tturnip"}

# pkg -> shared internal-storage dir it owns (mirror of lib-root.sh:internal_for). Restored only if the
# app is in the manifest.
INTERNAL_FOR = {
    "org.es_de.frontend": "ES-DE",
    "org.citra.emu": "citra-emu",
    "com.retroarch.aarch64": "RetroArch",
}


def internal_for(pkg):
    return INTERNAL_FOR.get(pkg)


# --- managed-APK server store -------------------------------------------------------------------
# A central, library-side APK store (config.apk_store_dir(), default library_root()/_apks): ONE current
# version of each package deploys, and every config that lists the app (apk axis) installs it. Captured
# golden APKs (golden_root_payload/<pkg>/apk) are SEPARATE and unchanged — the resolver prefers them.
def apk_store_pkg_dir(store_dir, pkg):
    """The store directory for one package: <store_dir>/<pkg>."""
    return pathlib.Path(store_dir) / pkg


def store_current_label(store_dir, pkg):
    """The 'current=' label from <store>/<pkg>/meta, or None when the package has no current build (never
    added, or soft-removed)."""
    return _read_meta(apk_store_pkg_dir(store_dir, pkg) / "meta").get("current") or None


def store_apk_files(store_dir, pkg):
    """APK file(s) for the package's CURRENT label: [<label>.apk] for a single build, or every *.apk under
    <label>/ (sorted) for a split build. [] if there's no current label or its file(s) are missing."""
    label = store_current_label(store_dir, pkg)
    if not label:
        return []
    d = apk_store_pkg_dir(store_dir, pkg)
    single = d / f"{label}.apk"
    if single.is_file():
        return [single]
    split = d / label
    if split.is_dir():
        return sorted(split.glob("*.apk"))
    return []


def list_store_apks(store_dir):
    """Every package in the store WITH a current build: [{'pkg','label','nfiles','bytes'}], sorted by pkg.
    Soft-removed packages (no current) and bookkeeping dirs (names starting with '_') are omitted."""
    root = pathlib.Path(store_dir)
    out = []
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        entries = []
    for d in entries:
        if not d.is_dir() or d.name.startswith("_"):
            continue
        files = store_apk_files(store_dir, d.name)
        if not files:
            continue
        # Best-effort byte sum — ignores stat errors (NAS files may vanish between listing and stat).
        total = 0
        for f in files:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        out.append({"pkg": d.name, "label": store_current_label(store_dir, d.name),
                    "nfiles": len(files), "bytes": total})
    return out


def _archive_if_exists(target, pkgdir):
    """Move an existing store target (file or dir) into <pkgdir>/_archive/ under a non-colliding name, so a
    re-used label never hard-deletes the prior bytes."""
    target = pathlib.Path(target)
    if not target.exists():
        return
    arch = pathlib.Path(pkgdir) / "_archive"
    arch.mkdir(parents=True, exist_ok=True)
    dest, n = arch / target.name, 1
    while dest.exists():
        dest = arch / f"{target.name}.{n}"
        n += 1
    shutil.move(str(target), str(dest))


def put_store_apk(store_dir, pkg, src, label=None):
    """Add/replace the CURRENT build of `pkg`. `src` is a single .apk file OR a directory of split APKs.
    Copies it under <store>/<pkg>/<label>(.apk|/) and repoints meta 'current=<label>'. `label` defaults to
    the source's filename stem (its dir name for a split set). Any PRIOR bytes occupying the same label
    target are archived first (never hard-deleted). Backs BOTH the GUI's Add and Update. Returns the label."""
    src = pathlib.Path(src)
    d = apk_store_pkg_dir(store_dir, pkg)
    d.mkdir(parents=True, exist_ok=True)
    label = label or (src.stem if src.is_file() else src.name)
    if src.is_dir():
        target = d / label
        _archive_if_exists(target, d)
        shutil.copytree(src, target)
    else:
        target = d / f"{label}.apk"
        _archive_if_exists(target, d)
        shutil.copy2(src, target)
    set_meta_key(d / "meta", "current", label)
    return label


def remove_store_apk(store_dir, pkg):
    """Soft-remove: clear meta 'current' so `pkg` stops deploying everywhere, while RETAINING every label
    file in place (re-running put_store_apk restores it). No-op if the package isn't in the store."""
    meta_path = apk_store_pkg_dir(store_dir, pkg) / "meta"
    if meta_path.exists():
        set_meta_key(meta_path, "current", "")


def resolve_app_apk(pkg, prof, store_dir, bundle_fallback=None):
    """The APK file(s) to install for `pkg`, in priority order, or None if nothing is available:
      1. the profile's CAPTURED module — golden_root_payload/<pkg>/apk/*.apk (unchanged behaviour),
      2. else the server store's CURRENT build (store_apk_files),
      3. else `bundle_fallback` — a path or list of paths shipped in the CAS bundle (kit apps only).
    Returns a list of file paths (the installer uses install-multiple when len > 1)."""
    if prof is not None:
        apkdir = pathlib.Path(prof.payload) / pkg / "apk"
        cap = sorted(apkdir.glob("*.apk")) if apkdir.is_dir() else []
        if cap:
            return cap
    files = store_apk_files(store_dir, pkg)
    if files:
        return files
    if bundle_fallback:
        cand = ([bundle_fallback] if isinstance(bundle_fallback, (str, pathlib.Path)) else bundle_fallback)
        fb = [pathlib.Path(p) for p in cand if pathlib.Path(p).is_file()]
        if fb:
            return fb
    return None


def download_rows(own_pkgs, store_pkgs, has_apk, has_config, always_install=None):
    """Golden-driven defaults for the Download app-pick modal. Returns (rows, cfg_disabled):
      * rows: ordered {pkg: (apk_default, cfg_default)}. A captured golden app defaults APK-ON only when the
        golden actually bundled an APK for it (has_apk[pkg]) — a config-only capture (APK sideloaded) defaults
        APK-OFF. Its Config defaults ON only when the golden captured config for it (has_config[pkg]) — an
        apk-only capture defaults Config-OFF. A store-only (managed) app — NOT in the golden — defaults
        APK-OFF (you opt in to push the store build) and has no captured config. FINALLY, any app in
        `always_install` (the global always-install set) has its APK default forced ON — for golden apps and
        for store-only apps alike — so operator-always-wanted apps install without re-ticking.
      * cfg_disabled: the set of pkgs whose Config checkbox the modal must DISABLE — you can't restore
        config that was never captured.
    Pure — the caller derives has_apk/has_config ({pkg: bool}) from the payload (Profile.has_captured_*)."""
    ai = always_install or frozenset()
    rows, cfg_disabled = {}, set()
    for pkg in own_pkgs:
        apk = bool(has_apk.get(pkg, True)) or (pkg in ai)   # always-install forces APK on
        cfg = bool(has_config.get(pkg))
        rows[pkg] = (apk, cfg)
        if not cfg:
            cfg_disabled.add(pkg)
    for pkg in store_pkgs:
        if pkg not in rows:
            rows[pkg] = ((pkg in ai), False)                # store-only member auto-ticks APK; else off
            cfg_disabled.add(pkg)
    return rows, cfg_disabled


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
    # write_BYTES, never write_text: text mode translates "\n" -> os.linesep, so on Windows this file went
    # to the device with CRLF. restore.sh/capture.sh parse it with awk, whose default field separator does
    # NOT include \r — every bare package line then yielded "$pkg" = "com.foo\r" and "$P/$pkg/apk/" named a
    # path that cannot exist, so Download warned "no APK in payload" for EVERY app while the payload was
    # sitting on the device, complete. (Python's str.split() strips \r, so PC-side validation passed on the
    # SAME file and the two sides disagreed.) The device is LF-only; keep these bytes LF-only.
    pathlib.Path(manifest_path).write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


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

    def launcher_pkg(self):
        """The golden's HOME launcher package (a device SYSTEM app — never an installable app row; its
        layout rides @homescreen). Resolved from profile.meta, else the captured homescreen/meta written at
        capture time. None when no homescreen was captured. SINGLE source of truth so the app list and its
        launcher-exclusion never disagree."""
        return (self.meta.get("launcher_pkg")
                or _read_meta(self.payload / "homescreen" / "meta").get("launcher_pkg")
                or None)

    def all_pkgs(self):
        """Every selectable app: the captured set (pkglist.txt) plus the default launcher (a system app
        excluded by user_pkgs) when known, so it can be ticked for config. The full toggle set for the UI."""
        pl = self.payload / "pkglist.txt"
        pkgs = ([l.strip() for l in _read_text(pl).splitlines() if l.strip()]
                if pl.exists() else self.pkgs())
        lp = self.launcher_pkg()
        if lp and lp not in pkgs:
            pkgs.append(lp)
        return pkgs

    def has_captured_apk(self, pkg):
        """True if the golden bundled an installable APK for pkg (golden_root_payload/<pkg>/apk/*.apk).
        False for store-only (managed) apps and config-only captures (APK sideloaded externally)."""
        apkdir = self.payload / pkg / "apk"
        return apkdir.is_dir() and any(apkdir.glob("*.apk"))

    def has_captured_config(self, pkg):
        """True if the golden captured restorable config/data for pkg — i.e. there is something for the
        Config axis to restore. capture.sh writes data.tar / adata.tar only when config was captured, so
        their presence is the ground truth. Store-only (managed) apps and apk-only captures have none."""
        d = self.payload / pkg
        return (d / "data.tar").exists() or (d / "adata.tar").exists()

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


def model_matches(model_match, model):
    """True when a profile's `model_match` fits a device's ro.product.model — the Root/Lock brick guard.

    Two passes:
      1. the raw regex, unchanged — hand-written patterns ('Odin2.*Mini', 'Retroid Pocket [56]') keep
         working, including alternation to cover several models with one profile.
      2. capacity-tolerant token subset: an operator who types the STORAGE TIER into the model
         ('Retroid Pocket 6 256') still fits a unit reporting plain 'Retroid Pocket 6' — ro.product.model
         never carries capacity, so a tier in the pattern is a typo, not a different device.

    Only numbers >= _CAP_MIN_GB are dropped as capacity, so a model VERSION stays significant: an RP5
    pattern never fits an RP6. That mismatch is the wrong-image flash this guard exists to stop.
    """
    mm, model = (model_match or "").strip(), (model or "").strip()
    if not mm or not model:
        return False                      # no pattern -> caller skips the guard; no model -> refuse (safe)
    try:
        if re.search(mm, model):
            return True
    except re.error:
        pass                              # malformed pattern -> fall through to tokens instead of raising
    want = {t for t in _toks(mm) if not _is_capacity(t)}
    have = {t for t in _toks(model) if not _is_capacity(t)}
    return bool(want) and want <= have


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


def default_capture_selection(device_apps, game_launcher=None, home_launcher=None, always_install=None):
    """The default Save-list check state: {pkg: (apk_on, config_on)}. Emulators (EMULATOR_PKGS) -> both axes,
    EXCEPT CONFIG_ONLY_PKGS (APK sideloaded externally) -> config-only; the game/HOME launcher -> config-only
    (APK is system firmware, but their state — emulator picks / homescreen — is worth keeping, so config
    defaults ON); every other device app -> off. Finally, any device app in `always_install` (the global
    always-install set) has its APK bit forced ON (Config left to the above policy) — these are apps the
    operator wants installed on every unit."""
    ai = always_install or frozenset()
    sel = {}
    for pkg in device_apps:
        if pkg in CONFIG_ONLY_PKGS:
            sel[pkg] = (False, True)                 # APK is provided externally -> capture config/BIOS only
        else:
            on = pkg in EMULATOR_PKGS
            sel[pkg] = (on, on)
    for lp in (game_launcher, home_launcher):
        if lp:
            # config-on by default (@gamelauncher / @homescreen). The APK bit follows whether the launcher
            # is actually INSTALLABLE: `device_apps` is `pm list -3`, so a launcher present there is
            # user-installed (e.g. the AYN Thor ships xyz.blacksheep.mjolnir as HOME) and its APK must be
            # captured — otherwise the target unit never gets it, set_home_component refuses, and the whole
            # @homescreen block (wallpaper included) is skipped. A launcher absent from the scan is system
            # firmware and stays APK-off, which is the case this rule originally existed for.
            sel[lp] = (lp in device_apps, True)
    for pkg in device_apps:                          # always-install: force APK on, keep the Config default
        if pkg in ai and pkg in sel:
            sel[pkg] = (True, sel[pkg][1])
    return sel


def initial_capture_selection(device_apps, saved_axes, saved_flags, game_launcher=None, home_launcher=None,
                              always_install=None):
    """The Save-list initial check state: default_capture_selection, overlaid by a saved capture-manifest's
    package axes, then the launcher rows seeded from the saved @gamelauncher/@homescreen flags. Members of
    `always_install` have their APK bit re-asserted ON *after* the saved overlay, so a stale saved manifest
    (APK previously unticked) can't suppress an always-install app. Pure — no I/O."""
    ai = always_install or frozenset()
    sel = default_capture_selection(device_apps, game_launcher, home_launcher, ai)
    # A saved manifest only OVERRIDES the axes of apps that are actually on this device — it never ADDS a
    # row. Capturing into a profile whose golden came from another unit must not surface apps this device
    # doesn't have (e.g. AetherSX2 on a Retroid that only ships NetherSX2). The scan is authoritative.
    for pkg, axes_pair in (saved_axes or {}).items():
        if pkg in sel:
            sel[pkg] = axes_pair
    # Sideloaded builds: the config-only policy WINS over a stale saved manifest that had APK on — the
    # operator never bundles their externally-installed APK (e.g. PS2) by accident. Config choice is kept.
    for pkg in CONFIG_ONLY_PKGS:
        if pkg in sel:
            sel[pkg] = (False, sel[pkg][1])
    # Always-install WINS over both the saved overlay and the config-only reassert above: force APK on.
    for pkg in ai:
        if pkg in sel:
            sel[pkg] = (True, sel[pkg][1])
    if game_launcher and game_launcher in sel:
        sel[game_launcher] = (False, (saved_flags or {}).get("gamelauncher", "on") == "on")
    if home_launcher and home_launcher in sel:
        # Preserve the APK bit decided above (on for a user-installed launcher, off for firmware) — only
        # the config bit carries the saved @homescreen flag.
        sel[home_launcher] = (sel[home_launcher][0], (saved_flags or {}).get("homescreen", "on") == "on")
    return sel


def toggle_always_member(current, pkg):
    """Toggle `pkg`'s membership in the global always-install set `current` (present -> removed, absent
    -> added); other members untouched. Returns a frozenset. Used by the Managed APKs window, which is
    where always-install membership is edited (per store-managed APK)."""
    s = set(current or ())
    s.discard(pkg) if pkg in s else s.add(pkg)
    return frozenset(s)


def archive_profile(profile, stamp, archive_root=None):
    """Soft-delete: MOVE the profile dir to profiles/_archive/<name>_<stamp>. Never rm. Returns dest."""
    src = pathlib.Path(profile.path)
    if archive_root is None:
        archive_root = src.parent / "_archive"
    dst = pathlib.Path(archive_root) / f"{profile.name}_{stamp}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst
