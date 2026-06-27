r"""Frozen GUI entry point (cas-gui.exe). Mirrors `python -m cas`.

PyInstaller freezes a *script*, not a `-m package` invocation, so this thin shim
reproduces cas/__main__.py: parse --adb/--fastboot, then call cas.gui.main(...).

Frozen-path note: when frozen, the operator normally double-clicks cas-gui.exe with
no args. We therefore AUTO-DETECT adb/fastboot from a sibling platform-tools\ dir
beside the executable (APPDIR), falling back to windows-kit\ (legacy), then PATH.
Explicit --adb/--fastboot always win.
"""
import argparse
import os
import sys

from cas.gui import main


def _appdir():
    # APPDIR = the writable/external root = the folder the executable lives in when
    # frozen, else the repo root (cwd-independent). Profiles + platform-tools sit here.
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _autodetect(tool, override):
    """Explicit override wins; else sibling platform-tools\\<tool>.exe, then legacy
    windows-kit\\<tool>.exe, then bare name on PATH."""
    if override:
        return override
    base = _appdir()
    exe = tool + (".exe" if os.name == "nt" else "")
    for sub in ("platform-tools", "windows-kit"):
        cand = os.path.join(base, sub, exe)
        if os.path.exists(cand):
            return cand
    return tool  # fall back to PATH


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="cas-gui",
                                 description="Console Auto Setup — provisioning front-end")
    ap.add_argument("--adb", default=None,
                    help="path to adb (default: auto-detect platform-tools\\adb.exe beside the "
                         "exe, else windows-kit\\adb.exe, else 'adb' on PATH)")
    ap.add_argument("--fastboot", default=None,
                    help="path to fastboot (same auto-detection as --adb; used by Seal to un-root)")
    args = ap.parse_args()
    main(adb_bin=_autodetect("adb", args.adb),
         fb_bin=_autodetect("fastboot", args.fastboot))
