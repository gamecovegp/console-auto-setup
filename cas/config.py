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
    """(user, password) from the config, or None if not set / unreadable."""
    cfg = load_config()
    u = cfg.get("nas_user")
    pw = cfg.get("nas_pw")
    if not u or pw is None:
        return None
    try:
        return (u, _xor(base64.b64decode(pw.encode())).decode())
    except Exception:
        return None


def nas_share_root():
    r"""The SMB share root to authenticate against, derived from NAS_DEFAULT: \\host\share."""
    parts = NAS_DEFAULT.lstrip("\\").split("\\")
    return ("\\\\" + parts[0] + "\\" + parts[1]) if len(parts) >= 2 else NAS_DEFAULT


def nas_connect(timeout=20):
    """Best-effort: authenticate to the NAS with the stored app account so the library path resolves.
    Windows uses `net use`; other OSes fall back to `gio mount`. Returns True if the library is reachable
    afterwards. Safe no-op when no creds are stored (False) or already connected (True)."""
    if library_reachable():
        return True
    creds = get_nas_credentials()
    if not creds:
        return False
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
