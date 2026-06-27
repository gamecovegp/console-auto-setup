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
The runtime siblings (data/ — profiles, Apps, ES-DE, cores — and platform-tools/) live OUTSIDE the
bundle, so a swap never touches them.
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


def download_and_verify(url, dest, sha256="", opener=_urlopen, progress=None):
    """Stream `url` to `dest`; if `sha256` is given, verify it. Returns dest on success, else None.

    `progress(downloaded, total)` (optional) is called after every chunk so the UI can render a real
    percentage — `total` is the Content-Length (0 if the server didn't send one, i.e. indeterminate)."""
    try:
        h = hashlib.sha256()
        done = 0
        with opener(url, timeout=60) as r, open(dest, "wb") as f:
            try:
                total = int(r.headers.get("Content-Length", 0) or 0)
            except (AttributeError, ValueError, TypeError):
                total = 0
            if progress:
                progress(0, total)
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total or done)      # if size unknown, report done as the running total
        if sha256 and h.hexdigest().lower() != sha256.lower():
            return None
        return dest
    except Exception:
        return None


def _exe_name(platform):
    return "cas-gui.exe" if platform.startswith("win") else "cas-gui"


def stage_and_relaunch(zip_path, appdir=None, log=print, platform=None, launch=None):
    """Extract the new bundle next to the current one and launch an OS helper that, once THIS process
    exits, OVERWRITES the bundle in place and relaunches the GUI. Returns True if a swap was handed off.

    Returns False (and logs WHY) instead of silently pretending to update when there is nothing to swap:
      * appdir has no cas-gui[.exe]      -> not a frozen release bundle (e.g. a `python -m cas` checkout,
                                            or a macOS .app whose binaries live inside Contents/). The old
                                            code launched a helper that copied nothing useful and STILL
                                            returned True — the exact "downloads but stays the same" bug.
      * the downloaded zip has no exe    -> malformed asset; don't clobber the install with garbage.

    The swap is deferred to a detached helper because a running executable can't overwrite itself. The
    helper writes <appdir>/cas-update.log so a failed swap on the bench is diagnosable, not a black box.
    """
    platform = platform or sys.platform
    launch = launch or _launch_detached
    try:
        appdir = pathlib.Path(appdir) if appdir else pathlib.Path(sys.executable).resolve().parent
        exe = _exe_name(platform)
        if not (appdir / exe).exists():
            log(f"update: no {exe} in {appdir} — in-place update only works on a frozen release bundle. "
                f"For a source checkout, update with `git pull` (or update.sh / update-win.bat).")
            return False
        staging = pathlib.Path(tempfile.mkdtemp(prefix="cas-update-"))
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(staging)
        # the zip contains a top-level `cas/` dir (the bundle); fall back to the staging root.
        new_bundle = staging / "cas"
        if not new_bundle.is_dir():
            new_bundle = staging
        if not (new_bundle / exe).exists():
            log(f"update: downloaded bundle has no {exe} (malformed asset) — not swapping.")
            return False
        helper = _write_helper(appdir, new_bundle, platform=platform, log=log)
        if helper is None:
            return False
        launch(helper, platform=platform)
        return True
    except Exception as e:
        log(f"update staging failed: {e}")
        return False


def _write_helper(appdir, new_bundle, platform=None, log=print):
    """Write the wait-swap-relaunch helper for the target OS. Returns its path.

    The helper (a) waits for THIS process (pid) to exit, (b) overwrite-copies the new bundle over the
    install — never purging, so external siblings (data/ — profiles, Apps, ES-DE, cores — and
    platform-tools/) survive — and (c) relaunches the GUI, logging every step to <appdir>/cas-update.log.
    """
    platform = platform or sys.platform
    pid = os.getpid()
    if platform.startswith("win"):
        gui = appdir / "cas-gui.exe"
        log_path = appdir / "cas-update.log"
        bat = pathlib.Path(tempfile.gettempdir()) / "cas-apply-update.bat"
        # ping -n (NOT timeout): timeout reads the console input handle and aborts with "Input redirection
        # is not supported" when the bat is launched console-less from the windowed exe — which silently
        # broke the wait loop. `ping -n 2 127.0.0.1` is a ~1s console-independent sleep.
        # robocopy /E (NOT /MIR): overwrite-copy, never DELETE extras, so the external siblings survive.
        # /R:2 /W:2 so a momentarily-locked file retries briefly instead of failing the whole swap.
        bat.write_text(
            "@echo off\r\n"
            "setlocal\r\n"
            f'set "LOG={log_path}"\r\n'
            'echo [cas-update] start %DATE% %TIME%> "%LOG%"\r\n'
            f'echo waiting for PID {pid} to exit>> "%LOG%"\r\n'
            ':wait\r\n'
            f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
            'if not errorlevel 1 (\r\n'
            '  ping -n 2 127.0.0.1 >nul\r\n'
            '  goto wait\r\n'
            ')\r\n'
            f'echo copying new bundle into "{appdir}">> "%LOG%"\r\n'
            f'robocopy "{new_bundle}" "{appdir}" /E /R:2 /W:2 /NFL /NDL /NJH /NJS >> "%LOG%"\r\n'
            'set RC=%ERRORLEVEL%\r\n'
            'echo robocopy exit code %RC%>> "%LOG%"\r\n'
            'if %RC% GEQ 8 (echo ERROR: swap FAILED ^(robocopy %RC%^) — version unchanged>> "%LOG%") '
            'else (echo OK: bundle swapped>> "%LOG%")\r\n'
            f'echo relaunching "{gui}">> "%LOG%"\r\n'
            f'start "" "{gui}"\r\n'
            'del "%~f0"\r\n')
        return bat
    gui = appdir / "cas-gui"
    log_path = appdir / "cas-update.log"
    sh = pathlib.Path(tempfile.gettempdir()) / "cas-apply-update.sh"
    sh.write_text(
        "#!/bin/sh\n"
        f'LOG="{log_path}"\n'
        'echo "[cas-update] start" > "$LOG"\n'
        f'echo "waiting for pid {pid} to exit" >> "$LOG"\n'
        f'while kill -0 {pid} 2>/dev/null; do sleep 1; done\n'
        f'echo "copying new bundle into {appdir}" >> "$LOG"\n'
        f'if cp -a "{new_bundle}/." "{appdir}/" >> "$LOG" 2>&1; then\n'
        '  echo "OK: bundle swapped" >> "$LOG"\n'
        'else\n'
        '  echo "ERROR: swap FAILED (cp) — version unchanged" >> "$LOG"\n'
        'fi\n'
        f'echo "relaunching {gui}" >> "$LOG"\n'
        f'"{gui}" >> "$LOG" 2>&1 &\n'
        f'rm -- "$0"\n')
    sh.chmod(0o755)
    return sh


def _launch_detached(helper, platform=None):
    platform = platform or sys.platform
    if platform.startswith("win"):
        # CREATE_NO_WINDOW (hidden console, so tasklist/ping/robocopy still work) + NEW_PROCESS_GROUP so
        # the helper outlives this exe. NOT DETACHED_PROCESS: that leaves the bat console-less, which broke
        # its console child tools. close_fds so no handle on the old bundle keeps a file locked during swap.
        CREATE_NO_WINDOW, NEW_PROCESS_GROUP = 0x08000000, 0x00000200
        subprocess.Popen(["cmd", "/c", str(helper)],
                         creationflags=CREATE_NO_WINDOW | NEW_PROCESS_GROUP, close_fds=True)
    else:
        subprocess.Popen(["/bin/sh", str(helper)], start_new_session=True)
