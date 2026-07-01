"""Where the CAS profile library lives. Resolves the library root from (priority):
  1. CAS_PROFILES env var   (one-shot override for scripts/CI)
  2. 'library' key in cas-config.json   (set via the GUI / persisted)
  3. the NAS share (NAS_DEFAULT) if it's mounted/reachable   <- "always store on the NAS"
  4. APPDIR/data/profiles   (local fallback when the NAS isn't mounted, e.g. a dev/offline machine)
The SMB share is normally mounted by the OS. Optionally, a dedicated low-privilege NAS app account can be
stored here (obfuscated) so CAS authenticates to the share itself (nas_connect) — no manual drive-mapping."""
import base64
import json
import os
import pathlib
import re
import socket
import subprocess
import sys

from . import APPDIR

# The shared golden library on the office NAS. On an authenticated Windows bench this UNC path resolves
# with no drive-letter mapping; on a machine where the share isn't mounted (this dev box, offline) it
# simply isn't a directory, so library_root() falls back to the local profiles dir.
NAS_DEFAULT = r"\\192.168.100.227\01 GAMECOVE\[03] SETUP\CAS Profiles"


def nas_default_path():
    """The NAS library path on THIS OS: the discovered share mountpoint + the subpath, or None when the
    share isn't mounted. Replaces the old hardcoded UNC/POSIX constants so the path follows wherever the OS
    mounted the share."""
    mp = nas_mountpoint()
    if not mp:
        return None
    sub = nas_subpath()
    return str(pathlib.Path(mp) / sub) if sub else mp


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
    """The profile library directory: CAS_PROFILES env > config 'library' > NAS (if reachable) > local."""
    env = os.environ.get("CAS_PROFILES")
    if env:
        return pathlib.Path(env)
    lib = load_config().get("library")
    if lib:
        return pathlib.Path(lib)
    # Default to the shared NAS library when the share is mounted (path discovered per-OS); else local.
    nas = nas_default_path()
    if nas:
        try:
            if pathlib.Path(nas).is_dir():
                return pathlib.Path(nas)
        except OSError:
            pass
    return APPDIR / "data" / "profiles"


def set_library(path):
    """Persist (path) or, with a falsy path, CLEAR the library override in cas-config.json — clearing makes
    library_root() follow the NAS default when it's mounted (local only when offline). Returns the resolved
    library_root()."""
    cfg = load_config()
    if path:
        cfg["library"] = str(path)
    else:
        cfg.pop("library", None)
    save_config(cfg)
    return library_root()


def history_dir(default=None):
    """Where the run-history .jsonl logs (download-history / save-history) are written:
      1. the shared 'log_dir' from cas-config.json   — IF set AND currently reachable (e.g. the NAS, so logs
         centralize across benches even while the heavy goldens stay on a fast LOCAL library), else
      2. `default` (the caller's library root), else library_root().
    Falls back to local when the NAS log dir is unreachable, so a run is never lost — the caller logs WHERE
    it actually landed."""
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
    path currently exists (so a stale NAS-pinned override on an offline bench is ignored and the catalog
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
    currently exists (so a stale NAS-pinned override on an offline bench is ignored and the store follows the
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


def library_reachable():
    """True if the configured library path exists as a directory (e.g. the NAS drive is mapped)."""
    try:
        return library_root().is_dir()
    except OSError:
        return False


# --------------------------------------------------------------------------------------------------
# Optional: let CAS authenticate to the NAS itself with a dedicated app account, so a fresh PC needs no
# manual drive-mapping. The password is obfuscated (NOT encrypted) in the config — use a low-privilege
# account scoped to the CAS Profiles share so a leaked file can't reach anything else.
# --------------------------------------------------------------------------------------------------
_OBF = b"cas/gamecove/nas/v1"

# Default NAS app-account SHIPPED with CAS, so a fresh bench PC auto-connects to the shared library with no
# manual login. SECURITY: this must be a DEDICATED, LOW-PRIVILEGE NAS account scoped ONLY to the CAS
# Profiles share on the LAN NAS (192.168.100.227, not internet-exposed) — anyone with the bundle/source can
# read it, so it must be able to reach nothing else. An operator-saved account (Settings → NAS login)
# OVERRIDES this default. Set NAS_DEFAULT_USER = "" to ship with no default.
NAS_DEFAULT_USER = "console-auto-setup"
NAS_DEFAULT_PW = "Auto-Setup123#"


def _xor(b):
    return bytes(c ^ _OBF[i % len(_OBF)] for i, c in enumerate(b))


def set_nas_credentials(user, password):
    """Store (or, with empty strings, clear) the NAS app-account creds in cas-config.json. The password is
    obfuscated, not encrypted — only meaningful for a dedicated, low-privilege account."""
    cfg = load_config()
    if user or password:
        cfg["nas_user"] = user
        cfg["nas_pw"] = base64.b64encode(_xor(password.encode())).decode()
    else:
        cfg.pop("nas_user", None)
        cfg.pop("nas_pw", None)
    save_config(cfg)


def get_nas_credentials():
    """(user, password) for the NAS: an operator-saved account from cas-config.json if present, else the
    shipped default app account (NAS_DEFAULT_USER/PW) so a fresh PC auto-connects. None only if there is
    no saved account AND no shipped default."""
    cfg = load_config()
    u = cfg.get("nas_user")
    pw = cfg.get("nas_pw")
    if u and pw is not None:
        try:
            return (u, _xor(base64.b64decode(pw.encode())).decode())
        except Exception:
            pass
    return (NAS_DEFAULT_USER, NAS_DEFAULT_PW) if NAS_DEFAULT_USER else None


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


def nas_share_root():
    r"""The SMB share root to authenticate against, derived from NAS_DEFAULT: \\host\share."""
    parts = NAS_DEFAULT.lstrip("\\").split("\\")
    return ("\\\\" + parts[0] + "\\" + parts[1]) if len(parts) >= 2 else NAS_DEFAULT


def nas_share_name():
    r"""The SMB share name from NAS_DEFAULT — the segment after the host (e.g. '01 GAMECOVE')."""
    parts = NAS_DEFAULT.lstrip("\\").split("\\")
    return parts[1] if len(parts) >= 2 else ""


def nas_subpath():
    r"""The path UNDER the share from NAS_DEFAULT, POSIX-separated (e.g. '[03] SETUP/CAS Profiles')."""
    parts = NAS_DEFAULT.lstrip("\\").split("\\")
    return "/".join(parts[2:]) if len(parts) > 2 else ""


def nas_mountpoint():
    """The local path of the NAS SHARE ROOT on THIS OS (to which nas_subpath() is appended), or None if the
    share is not mounted. Discovered by existence, never hardcoded: Windows -> the UNC; macOS ->
    /Volumes/<share>; Linux -> the conventional gvfs FUSE path gio mounts the share at."""
    share = nas_share_name()
    if not share:
        return None
    try:
        if sys.platform == "win32":
            unc = nas_share_root()
            return unc if pathlib.Path(unc).is_dir() else None
        if sys.platform == "darwin":
            p = pathlib.Path("/Volumes") / share
            return str(p) if p.is_dir() else None
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        p = pathlib.Path(runtime) / "gvfs" / f"smb-share:server={nas_host()},share={share.lower()}"
        if p.is_dir():
            return str(p)
        # gvfs is the desktop case; also honor a KERNEL CIFS/SMB mount (mount.cifs / fstab, e.g. the share
        # mounted at /mnt/gamecove) so the library follows the NAS without a manual per-dir override.
        mp = _linux_cifs_mountpoint(nas_host(), share)
        return mp if (mp and pathlib.Path(mp).is_dir()) else None
    except OSError:
        return None


def _unescape_mount(s):
    r"""Decode /proc/mounts octal escapes (space -> \040, tab -> \011, backslash -> \134, …)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _linux_cifs_mountpoint(host, share, mounts_path="/proc/mounts"):
    """Mountpoint of a kernel CIFS/SMB mount of //host/share from /proc/mounts, or None. Covers the common
    mount.cifs/fstab case (e.g. //192.168.100.227/01 GAMECOVE at /mnt/gamecove) that the gvfs check misses.
    Octal-unescapes the space in a share name like '01 GAMECOVE'. Pure parse — the caller checks is_dir()."""
    want = f"//{host}/{share}".replace("\\", "/").lower().rstrip("/")
    try:
        with open(mounts_path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or parts[2] not in ("cifs", "smb3", "smbfs"):
            continue
        if _unescape_mount(parts[0]).replace("\\", "/").lower().rstrip("/") == want:
            return _unescape_mount(parts[1])
    return None


def nas_host():
    """The NAS hostname/IP from NAS_DEFAULT (e.g. '192.168.100.227')."""
    return NAS_DEFAULT.lstrip("\\").split("\\")[0]


def nas_reachable(timeout=1.5):
    """Fast TCP probe of the NAS SMB port — so startup auto-connect doesn't BLOCK for the full mount
    timeout on a machine that isn't on the NAS network (now that a default account always exists)."""
    try:
        with socket.create_connection((nas_host(), 445), timeout=timeout):
            return True
    except OSError:
        return False


def nas_connect(timeout=20):
    """Best-effort: authenticate to the NAS with the saved/default app account so the library path resolves.
    Windows uses `net use`; other OSes fall back to `gio mount`. Returns True if the library is reachable
    afterwards. Safe no-op when already connected (True), no creds (False), or the NAS isn't on this
    network (False, fast — via nas_reachable)."""
    if nas_mountpoint():
        return True
    creds = get_nas_credentials()
    if not creds:
        return False
    if not nas_reachable():
        return False                                    # NAS not on this network — skip the slow mount
    user, pw = creds
    try:
        if sys.platform == "win32":
            share = nas_share_root()
            no_window = 0x08000000  # CREATE_NO_WINDOW — don't flash a console from the windowed GUI
            subprocess.run(["net", "use", share, "/delete", "/y"],
                           capture_output=True, timeout=timeout, creationflags=no_window)
            # /persistent:yes -> Windows restores the mapping on every login, so the library SURVIVES REBOOTS
            # (CAS also re-runs this on startup as a belt-and-braces). The default app account is shipped, so
            # this needs no per-bench setup.
            r = subprocess.run(["net", "use", share, pw, "/user:" + user, "/persistent:yes"],
                               capture_output=True, text=True, timeout=timeout, creationflags=no_window)
            if r.returncode != 0:
                return False
        elif sys.platform == "darwin":
            from urllib.parse import quote
            mp = pathlib.Path("/Volumes") / nas_share_name()
            mp.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["mount_smbfs",
                 f"//{quote(user)}:{quote(pw)}@{nas_host()}/{quote(nas_share_name())}", str(mp)],
                capture_output=True, text=True, timeout=timeout)
        else:
            from urllib.parse import quote
            url = f"smb://{nas_host()}/{quote(nas_share_name())}"   # mount the SHARE; discovery appends subpath
            subprocess.run(["gio", "mount", url], input=f"{user}\n\n{pw}\n",
                           text=True, capture_output=True, timeout=timeout)
        return nas_mountpoint() is not None
    except Exception:
        return False
