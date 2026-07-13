"""Where the CAS profile library lives. Resolves the library root from (priority):
  1. CAS_PROFILES env var   (one-shot override for scripts/CI)
  2. 'library' key in cas-config.json   (set via the GUI 'Library folder…' picker)
  3. APPDIR/data/profiles   (local default)
The library is a local/external drive folder — set it once per bench (Settings -> Library folder)."""
import json
import os
import pathlib
import re
import socket

from . import APPDIR


def config_path():
    """cas-config.json next to the app (override with CAS_CONFIG, mainly for tests)."""
    return pathlib.Path(os.environ.get("CAS_CONFIG", str(APPDIR / "cas-config.json")))


def load_config():
    """Parsed config dict, or {} if the file is missing or unparseable."""
    try:
        return json.loads(config_path().read_text())
    except Exception:
        return {}


def save_config(cfg):
    """Write cfg as pretty-printed JSON to config_path() (creating its parent dir if missing)."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2))


def library_root():
    """The profile library directory: CAS_PROFILES env > config 'library' > local (APPDIR/data/profiles)."""
    env = os.environ.get("CAS_PROFILES")
    if env:
        return pathlib.Path(env)
    lib = load_config().get("library")
    if lib:
        return pathlib.Path(lib)
    return APPDIR / "data" / "profiles"


def set_library(path):
    """Persist (path) or, with a falsy path, CLEAR the library override in cas-config.json — clearing makes
    library_root() fall back to the local default (APPDIR/data/profiles). Returns the resolved
    library_root()."""
    cfg = load_config()
    if path:
        cfg["library"] = str(path)
    else:
        cfg.pop("library", None)
    save_config(cfg)
    return library_root()


_DEFAULT_ALWAYS_INSTALL = ("com.valvesoftware.steamlink", "com.gamecove.gamecove_companion")


def always_install_pkgs():
    """The global 'always-install' package set (frozenset) — apps pre-ticked APK-on in the Save dialog
    and auto-ticked APK-on in the Download dialog for every profile. An explicit 'always_install' list in
    cas-config.json overrides the default; a stored empty list disables the feature."""
    v = load_config().get("always_install")
    if isinstance(v, list):
        return frozenset(str(p) for p in v)
    return frozenset(_DEFAULT_ALWAYS_INSTALL)


def set_always_install_pkgs(pkgs):
    """Persist the always-install set. `pkgs is None` CLEARS the override (getter falls back to the default
    set). A list/iterable — INCLUDING an empty one — is stored verbatim (sorted, deduped): an empty list
    DISABLES the feature. A bare string is treated as a single pkg id. Returns always_install_pkgs()."""
    cfg = load_config()
    if pkgs is None:
        cfg.pop("always_install", None)
    else:
        if isinstance(pkgs, str):
            pkgs = [pkgs]
        cfg["always_install"] = sorted({str(p) for p in pkgs})
    save_config(cfg)
    return always_install_pkgs()


def history_dir(default=None):
    """Where the run-history .jsonl logs (download-history / save-history) are written:
      1. the shared 'log_dir' from cas-config.json   — IF set AND currently reachable (e.g. a shared folder,
         so logs centralize across benches even while the heavy goldens stay on a fast LOCAL library), else
      2. `default` (the caller's library root), else library_root().
    Falls back to local when the shared log dir is unreachable, so a run is never lost — the caller logs
    WHERE it actually landed."""
    d = load_config().get("log_dir")
    if d:
        p = pathlib.Path(d)
        try:
            if p.is_dir():
                return p
        except OSError:
            pass
    return pathlib.Path(default) if default is not None else library_root()


def set_log_dir(path):
    """Persist (path) or clear (falsy) the shared log directory for the run-history .jsonl logs."""
    cfg = load_config()
    if path:
        cfg["log_dir"] = str(path)
    else:
        cfg.pop("log_dir", None)
    save_config(cfg)
    return load_config().get("log_dir")


def firmware_dir():
    """The device-root-firmware library directory. An explicit 'firmware_dir' override is honored ONLY if its
    path currently exists (so a stale override on an offline bench is ignored and the catalog
    follows the discovered library); otherwise library_root()/_firmware. Mirrors history_dir's log_dir rule."""
    d = load_config().get("firmware_dir")
    if d:
        p = pathlib.Path(d)
        try:
            if p.is_dir():
                return p
        except OSError:
            pass
    return library_root() / "_firmware"


def set_firmware_dir(path):
    """Persist (path) or clear (falsy) the firmware-library directory."""
    cfg = load_config()
    if path:
        cfg["firmware_dir"] = str(path)
    else:
        cfg.pop("firmware_dir", None)
    save_config(cfg)
    return load_config().get("firmware_dir")


def apk_store_dir():
    """The managed-APK server store directory. An explicit 'apk_store' override is honored ONLY if its path
    currently exists (so a stale override on an offline bench is ignored and the store follows the
    discovered library); otherwise library_root()/_apks. Mirrors firmware_dir's rule, so 'on the server by
    default' needs no extra wiring."""
    d = load_config().get("apk_store")
    if d:
        p = pathlib.Path(d)
        try:
            if p.is_dir():
                return p
        except OSError:
            pass
    return library_root() / "_apks"


def set_apk_store(path):
    """Persist (path) or clear (falsy) the managed-APK store directory."""
    cfg = load_config()
    if path:
        cfg["apk_store"] = str(path)
    else:
        cfg.pop("apk_store", None)
    save_config(cfg)
    return load_config().get("apk_store")


def es_media_src():
    """PC folder to push ES-DE box art FROM, or None to use the SD card (default, no per-unit push).
    Priority: CAS_MEDIA env (one-shot override) > config 'es_media_src' > None. None => SD mode: nothing is
    transferred and restore points ES-DE's MediaDirectory at the unit's own SD card instead."""
    env = os.environ.get("CAS_MEDIA")
    if env:
        return env
    return load_config().get("es_media_src") or None


def set_es_media_src(path):
    """Persist the ES-DE box-art PC source folder, or clear it (falsy => 'use the SD card').
    Returns the resolved es_media_src()."""
    cfg = load_config()
    if path:
        cfg["es_media_src"] = str(path)
    else:
        cfg.pop("es_media_src", None)
    save_config(cfg)
    return es_media_src()


def auto_grant_shell():
    """Whether root() auto-grants + persists the MagiskSU shell grant with no human tap (default
    True). Set "auto_grant_shell": false in cas-config.json to fall back to the manual Magisk
    Superuser toggle."""
    return bool(load_config().get("auto_grant_shell", True))


_DEFAULT_WARMUP_DWELL_S = 3.0
_DEFAULT_WARMUP_SKIP = ("com.topjohnwu.magisk",)


def warmup_dwell_s():
    """Seconds the ③ Warm up step leaves each app in the foreground before launching the next (default
    3.0). Apps are never force-stopped, so this bounds how long we WATCH an app, not how long it gets to
    index — a backgrounded app keeps scanning. Raise it (cas-config.json "warmup_dwell_s") if a unit still
    ships with an unindexed emulator. A garbage/negative value falls back to the default / 0."""
    try:
        return max(0.0, float(load_config().get("warmup_dwell_s", _DEFAULT_WARMUP_DWELL_S)))
    except (TypeError, ValueError):
        return _DEFAULT_WARMUP_DWELL_S


def warmup_skip_pkgs():
    """Packages ③ Warm up never launches (frozenset). Default: Magisk ONLY — it's a host tool, not a
    shipped app, so opening it does nothing for the unit. EVERYTHING else warms (Companion, Steam Link,
    every emulator): at 3s an app, an unnecessary launch costs 3 seconds, and a blanket rule is cheaper to
    reason about than a curated list. A stored list overrides; a stored EMPTY list skips nothing."""
    v = load_config().get("warmup_skip_pkgs")
    if isinstance(v, list):
        return frozenset(str(p) for p in v)
    return frozenset(_DEFAULT_WARMUP_SKIP)


# --- per-device profile memory ------------------------------------------------------------------
# A device is identified by its adb SERIAL (the unit's stable hardware serial — survives reboot/reflash and
# SD swaps). We remember each device's profile so it sticks across launches: the FIRST time a device is
# seen it's auto-matched and saved; an operator override is saved with manual=True (and always wins).
def get_device_profiles():
    """{serial: {'profile': name, 'manual': bool}} — remembered per-device profile assignments."""
    raw = load_config().get("device_profiles")
    out = {}
    if isinstance(raw, dict):
        for serial, v in raw.items():
            if isinstance(v, dict) and v.get("profile"):
                out[serial] = {"profile": str(v["profile"]), "manual": bool(v.get("manual"))}
            elif isinstance(v, str) and v:                     # tolerate a bare {serial: name} shape
                out[serial] = {"profile": v, "manual": True}
    return out


def set_device_profile(serial, profile, manual=True):
    """Remember a device's profile (profile truthy) or forget it (falsy). manual=True marks an operator
    override (sticky + tinted); manual=False is a remembered first-find auto-match."""
    if not serial:
        return
    cfg = load_config()
    dp = cfg.get("device_profiles")
    if not isinstance(dp, dict):
        dp = {}
    if profile:
        dp[serial] = {"profile": str(profile), "manual": bool(manual)}
    else:
        dp.pop(serial, None)
    cfg["device_profiles"] = dp
    save_config(cfg)


# --- download throughput history (for the per-profile download-time estimate) -------------------
def record_download(nbytes, seconds, profile=None, serial=None, model=None):
    """Record one Download: payload bytes, seconds, and WHICH profile / device (serial + model) + when.
    Drives the averaged download-time ETA and a per-device/per-profile history. Keeps the last 50 samples.
    No-op on zero/negative size or time."""
    if not nbytes or seconds is None or seconds <= 0:
        return
    cfg = load_config()
    stats = cfg.get("download_stats")
    if not isinstance(stats, list):
        stats = []
    rec = {"bytes": int(nbytes), "secs": round(float(seconds), 2)}
    if profile:
        rec["profile"] = str(profile)
    if serial:
        rec["serial"] = str(serial)
    if model:
        rec["model"] = str(model)
    try:
        import datetime
        rec["when"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    stats.append(rec)
    cfg["download_stats"] = stats[-50:]
    save_config(cfg)


def download_stats():
    """The recorded Download history (list of {bytes, secs, profile, serial, model, when}), newest last."""
    s = load_config().get("download_stats")
    return [r for r in s if isinstance(r, dict)] if isinstance(s, list) else []


def download_mbps(profile=None):
    """Average Download throughput (MB/s). With `profile`, prefer THAT profile's own samples (tighter
    estimate), falling back to all samples. None if nothing usable is recorded yet."""
    sel = download_stats()
    if profile:
        own = [s for s in sel if s.get("profile") == profile]
        if own:
            sel = own
    tot_b = sum(s.get("bytes", 0) for s in sel)
    tot_s = sum(s.get("secs", 0) for s in sel)
    if tot_b <= 0 or tot_s <= 0:
        return None
    return (tot_b / 1048576.0) / tot_s


def machine_tag():
    """A filesystem-safe per-machine tag (the sanitized hostname) used to namespace the run-history logs, so
    multiple benches that sync the library by whole-directory copy-paste never clobber each other's
    (write-only) audit logs. Lowercased; any run of non-[A-Za-z0-9._-] -> '-'; 'unknown' if empty."""
    try:
        raw = socket.gethostname() or ""
    except OSError:
        raw = ""
    tag = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.").lower()
    return tag or "unknown"


def history_filename(stem):
    """`<stem>.<machine_tag>.jsonl` — the per-machine run-history filename (copy-paste-safe across benches)."""
    return f"{stem}.{machine_tag()}.jsonl"


def library_reachable():
    """True if the configured library path exists as a directory (e.g. an external drive is connected)."""
    try:
        return library_root().is_dir()
    except OSError:
        return False


# Operator-only un-provision guard token. NOT a cryptographic secret (physical + USB-debug access is the
# real gate); it only stops a rogue on-device app from triggering release. MUST match the Companion app's
# res/values/cas_release.xml. Operator can override per-PC via cas-config.json ("release_token").
RELEASE_TOKEN_DEFAULT = "gc-release-7f3a9c2e"


def get_release_token():
    """The release guard token: an operator override from cas-config.json if present, else the shipped
    default (which matches the Companion build)."""
    cfg = load_config()
    t = cfg.get("release_token")
    return t if t else RELEASE_TOKEN_DEFAULT
