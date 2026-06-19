"""Launch the CAS GUI:  python -m cas  [--adb PATH_TO_ADB] [--fastboot PATH_TO_FASTBOOT]"""
import argparse

from . import find_adb
from .gui import main

if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="cas", description="Console Auto Setup — provisioning front-end")
    # Default None so an explicit override beats the sibling-platform-tools auto-detect below.
    ap.add_argument("--adb", default=None,
                    help="path to the adb executable (default: sibling platform-tools/adb, "
                         "else 'adb' on PATH; on Windows point at windows-kit\\adb.exe)")
    ap.add_argument("--fastboot", default=None,
                    help="path to the fastboot executable (used by Seal to un-root; default: "
                         "sibling platform-tools/fastboot, else 'fastboot' on PATH; "
                         "on Windows point at windows-kit\\fastboot.exe)")
    args = ap.parse_args()
    # --adb/--fastboot override wins; otherwise auto-detect APPDIR/platform-tools, falling back to PATH.
    adb_bin = args.adb or find_adb("adb")
    fb_bin = args.fastboot or find_adb("fastboot")
    main(adb_bin=adb_bin, fb_bin=fb_bin)
