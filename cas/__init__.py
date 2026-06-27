"""cas — Console Auto Setup: PC-side orchestrator + GUI for the root clone toolkit.

The device-side engine (provision/root/restore.sh, capture.sh, lib-root.sh) is unchanged and runs on
the device under su. This package drives adb, manages the profile library, and provides the Tkinter GUI.

Path model (frozen-aware). Two roots, equal in source mode, split when PyInstaller-frozen:

  BUNDLE  — read-only resources root. Bundled device-side scripts live at BUNDLE/provision/root/*.sh.
            Frozen: sys._MEIPASS (onefile temp-extract dir, or onedir base). Source: repo ROOT.
  APPDIR  — writable/external root. data/ (profiles, Apps, ES-DE, … — read-write, multi-GB) and
            external platform-tools/ live beside the executable. Frozen: dir of the executable (for a
            macOS .app bundle, the dir CONTAINING the .app). Source: repo ROOT. DATA == APPDIR/"data".

In SOURCE mode BUNDLE == APPDIR == repo ROOT, so existing behavior and the unit tests are unchanged.

ROOT stays defined (== APPDIR) for back-compat with any caller that still imports it.
"""
import os
import sys
import pathlib

# Repo root = parent of this package dir (…/console-auto-setup/) when running from source.
_SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent

_FROZEN = bool(getattr(sys, "frozen", False))


def _bundle_root():
    """Read-only resources root: PyInstaller's extract dir when frozen, else the repo root.

    _MEIPASS is set at runtime by both onefile and onedir bootloaders, but guard anyway and fall
    back to the executable's directory (onedir layout) so a bundle without it still resolves.
    """
    if _FROZEN:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return pathlib.Path(meipass).resolve()
        return pathlib.Path(sys.executable).resolve().parent
    return _SRC_ROOT


def _app_root():
    """Writable/external root: dir beside the executable when frozen, else the repo root.

    macOS .app special case: when frozen and the executable lives inside *.app/Contents/MacOS/,
    walk up three levels (MacOS -> Contents -> Foo.app -> the directory containing Foo.app) so
    profiles/ and platform-tools/ sit next to the .app, not buried inside it.
    """
    if not _FROZEN:
        return _SRC_ROOT
    exe = pathlib.Path(sys.executable).resolve()
    parts = exe.parts
    # …/Foo.app/Contents/MacOS/exe  -> parts[-2]=MacOS, parts[-3]=Contents, parts[-4]=Foo.app
    if len(parts) >= 4 and parts[-2] == "MacOS" and parts[-3] == "Contents" \
            and parts[-4].endswith(".app"):
        return exe.parent.parent.parent.parent
    return exe.parent


BUNDLE = _bundle_root()     # read-only bundled resources (device-side scripts)
APPDIR = _app_root()        # writable/external: data/, platform-tools/, cas-config.json
ROOT = APPDIR               # back-compat alias (was: repo root; still == repo root in source mode)
DATA = APPDIR / "data"      # operator-supplied runtime data, grouped under one dir beside the app:
#   data/profiles, data/Apps, data/ES-DE/downloaded_media, data/retroarch-cores, … . Source: repo/data.
#   Frozen: <beside-exe>/data. Build/update scripts stage these into dist/cas/data/.

__version__ = "0.1.0"


def find_adb(name):
    """Resolve an adb/fastboot binary for the default (no --adb/--fastboot override given).

    Prefer a sibling APPDIR/platform-tools/<name>[.exe] next to the executable so a frozen drop-in
    bundle is self-contained; otherwise return the bare name and let it resolve on PATH.

    `name` is the bare tool name ("adb" / "fastboot"); the platform's executable suffix
    (.exe on Windows) is appended automatically when probing the sibling dir.
    """
    exe = name + (".exe" if os.name == "nt" else "")
    candidate = APPDIR / "platform-tools" / exe
    if candidate.exists():
        return str(candidate)
    return name
