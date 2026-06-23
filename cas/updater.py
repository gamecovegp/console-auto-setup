"""Self-update against the public GitHub Release. Stdlib-only (urllib + zipfile + hashlib).

The release publishes a stable `latest.json` asset, reachable at a fixed URL via GitHub's
`releases/latest/download/<asset>` redirect — no API, no auth (the repo is public):

    https://github.com/<repo>/releases/latest/download/latest.json

    { "version": "0.2.0",
      "notes": "…",
      "assets": {
        "windows": { "url": "…/cas-windows.zip", "sha256": "…" },
        "linux":   { "url": "…/cas-linux.zip",   "sha256": "…" },
        "macos":   { "url": "…/cas-macos.zip",   "sha256": "…" } } }

`check()` (pure + network-injected) decides whether a newer build exists for THIS OS.
`download_and_verify()` fetches + sha256-checks the per-OS zip. `stage_and_relaunch()` extracts
it and hands off to a tiny OS helper that swaps the bundle once this process exits, then relaunches.
The runtime siblings (profiles/, Apps/, platform-tools/, cores) live OUTSIDE the bundle, so a swap
never touches them.
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

REPO = "gamecovegp/console-auto-setup"
LATEST_URL = f"https://github.com/{REPO}/releases/latest/download/latest.json"


def parse_version(s):
    """'v0.2.0' -> (0, 2, 0). Non-numeric junk in a part -> 0, so it always compares."""
    s = (s or "").strip().lstrip("vV")
    parts = []
    for p in s.split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(latest, current):
    return parse_version(latest) > parse_version(current)


def os_key(platform=None):
    """Manifest key for the running OS."""
    p = platform or sys.platform
    if p.startswith("win"):
        return "windows"
    if p == "darwin":
        return "macos"
    return "linux"


def _urlopen(url, timeout=10):
    return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 (fixed https GitHub URL)


def check(current_version, opener=_urlopen, url=LATEST_URL, os_name=None):
    """Return {version, url, sha256, notes} for THIS OS if a newer release exists, else None.
    NEVER raises — offline / bad JSON / missing asset all return None (telemetry-style)."""
    key = os_name or os_key()
    try:
        raw = opener(url, timeout=10).read()
        man = json.loads(raw)
        ver = str(man.get("version", "")).strip()
        if not ver or not is_newer(ver, current_version):
            return None
        asset = (man.get("assets") or {}).get(key) or {}
        if not asset.get("url"):
            return None
        return {"version": ver, "url": asset["url"],
                "sha256": str(asset.get("sha256", "")), "notes": str(man.get("notes", ""))}
    except Exception:
        return None


def download_and_verify(url, dest, sha256="", opener=_urlopen):
    """Stream `url` to `dest`; if `sha256` is given, verify it. Returns dest on success, else None."""
    try:
        h = hashlib.sha256()
        with opener(url, timeout=60) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
        if sha256 and h.hexdigest().lower() != sha256.lower():
            return None
        return dest
    except Exception:
        return None


def stage_and_relaunch(zip_path, appdir=None, log=print):
    """Extract the new bundle next to the current one and launch an OS helper that, once THIS
    process exits, replaces the bundle dir and relaunches the GUI. Returns True if handed off.

    NOTE: the swap+relaunch helper is OS-specific and must be verified on a real Windows bench
    before being relied on for unattended updates — a frozen exe can't overwrite itself while
    running, so the swap is deferred to the helper after this process exits.
    """
    try:
        appdir = pathlib.Path(appdir) if appdir else pathlib.Path(sys.executable).resolve().parent
        staging = pathlib.Path(tempfile.mkdtemp(prefix="cas-update-"))
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(staging)
        # the zip contains a top-level `cas/` dir (the bundle); fall back to the staging root.
        new_bundle = staging / "cas"
        if not new_bundle.is_dir():
            new_bundle = staging
        helper = _write_helper(appdir, new_bundle, log=log)
        if helper is None:
            return False
        _launch_detached(helper)
        return True
    except Exception as e:
        log(f"update staging failed: {e}")
        return False


def _write_helper(appdir, new_bundle, log=print):
    """Write the wait-swap-relaunch helper for the current OS. Returns its path."""
    pid = os.getpid()
    if sys.platform.startswith("win"):
        gui = appdir / "cas-gui.exe"
        bat = pathlib.Path(tempfile.gettempdir()) / "cas-apply-update.bat"
        bat.write_text(
            "@echo off\r\n"
            f':wait\r\n'
            f'tasklist /FI "PID eq {pid}" | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto wait)\r\n'
            # /E overwrite-copy (NOT /MIR): copy the new bundle over the old, never DELETE extras — so
            # the external siblings (profiles\, Apps\, platform-tools\, cores, ES-DE\) are left untouched.
            f'robocopy "{new_bundle}" "{appdir}" /E /NFL /NDL /NJH /NJS >nul\r\n'
            f'start "" "{gui}"\r\n'
            f'del "%~f0"\r\n')
        return bat
    gui = appdir / "cas-gui"
    sh = pathlib.Path(tempfile.gettempdir()) / "cas-apply-update.sh"
    sh.write_text(
        "#!/bin/sh\n"
        f'while kill -0 {pid} 2>/dev/null; do sleep 1; done\n'
        f'cp -a "{new_bundle}/." "{appdir}/"\n'
        f'"{gui}" &\n'
        f'rm -- "$0"\n')
    sh.chmod(0o755)
    return sh


def _launch_detached(helper):
    if sys.platform.startswith("win"):
        subprocess.Popen(["cmd", "/c", str(helper)],
                         creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_PROCESS_GROUP
    else:
        subprocess.Popen(["/bin/sh", str(helper)], start_new_session=True)
