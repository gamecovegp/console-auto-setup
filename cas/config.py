"""Where the CAS profile library lives. Resolves the library root from (priority):
  1. CAS_PROFILES env var   (one-shot override for scripts/CI)
  2. 'library' key in cas-config.json   (set via the GUI / persisted)
  3. the NAS share (NAS_DEFAULT) if it's mounted/reachable   <- "always store on the NAS"
  4. APPDIR/profiles        (local fallback when the NAS isn't mounted, e.g. a dev/offline machine)
The SMB share is normally mounted by the OS. Optionally, a dedicated low-privilege NAS app account can be
stored here (obfuscated) so CAS authenticates to the share itself (nas_connect) — no manual drive-mapping."""
import base64
import json
import os
import pathlib
import socket
import subprocess
import sys

from . import APPDIR

# The shared golden library on the office NAS. On an authenticated Windows bench this UNC path resolves
# with no drive-letter mapping; on a machine where the share isn't mounted (this dev box, offline) it
# simply isn't a directory, so library_root() falls back to the local profiles dir.
NAS_DEFAULT = r"\\192.168.100.227\01 GAMECOVE\[03] SETUP\CAS Profiles"
# On Linux/macOS that Windows UNC can't resolve; if the SMB share is mounted (cifs) at this conventional
# point, CAS auto-uses it. Mount once with:
#   sudo mount -t cifs '//192.168.100.227/01 GAMECOVE' /mnt/gamecove -o username=<app>,uid=$(id -u),...
NAS_DEFAULT_POSIX = "/mnt/gamecove/[03] SETUP/CAS Profiles"


def nas_default_path():
    """The OS-appropriate default NAS library path: the Windows UNC, else the POSIX cifs mount point."""
    return NAS_DEFAULT if sys.platform == "win32" else NAS_DEFAULT_POSIX


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
    # Default to the shared NAS library if it's mounted (UNC on Windows / cifs mount on POSIX); else local.
    nas = pathlib.Path(nas_default_path())
    try:
        if nas.is_dir():
            return nas
    except OSError:
        pass
    return APPDIR / "profiles"


def set_library(path):
    """Persist the library location to cas-config.json. Returns the resolved library_root()."""
    cfg = load_config()
    cfg["library"] = str(path)
    save_config(cfg)
    return library_root()


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
NAS_DEFAULT_PW = "console-setup"


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


def nas_share_root():
    r"""The SMB share root to authenticate against, derived from NAS_DEFAULT: \\host\share."""
    parts = NAS_DEFAULT.lstrip("\\").split("\\")
    return ("\\\\" + parts[0] + "\\" + parts[1]) if len(parts) >= 2 else NAS_DEFAULT


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
    if library_reachable():
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
            r = subprocess.run(["net", "use", share, pw, "/user:" + user],
                               capture_output=True, text=True, timeout=timeout, creationflags=no_window)
            if r.returncode != 0:
                return False
        else:
            url = "smb://" + NAS_DEFAULT.lstrip("\\").replace("\\", "/")
            subprocess.run(["gio", "mount", url], input=f"{user}\n\n{pw}\n",
                           text=True, capture_output=True, timeout=timeout)
        return library_reachable()
    except Exception:
        return False
