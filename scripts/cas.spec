# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for CAS (Console Auto Setup).
#
# Builds TWO executables that share ONE onedir bundle so they sit side by side:
#   cas-gui.exe   windowed GUI  (console=False)  <- python -m cas
#   cas.exe       console CLI   (console=True)   <- python -m cas.cli
#
# WHY onedir (COLLECT) and NOT onefile:
#   * onefile re-extracts the whole bundle to a temp dir on EVERY launch (slower start,
#     leaves temp churn). A frozen Tkinter GUI gains nothing from a single-file blob.
#   * The frozen-path contract needs profiles\ and platform-tools\ to live BESIDE the
#     executable (APPDIR = dir of the exe). With onedir, APPDIR is a stable, visible
#     dist\cas\ folder the operator drops those external dirs into. With onefile,
#     sys.executable points at a temp-extracted stub dir, so "beside the exe" is wrong.
#   * Two exes can share one COLLECT dir (one copy of the Python runtime + Tcl/Tk),
#     instead of two fat onefile blobs.
#
# WHY a .spec (not raw `pyinstaller --add-data ...`):
#   --add-data uses an OS-specific separator (';' on Windows, ':' on Unix). A .spec
#   takes a plain Python list of (src, dest) tuples, so there is no separator trap and
#   the build is identical regardless of who runs it.
#
# WHAT ships in the bundle (read-only, per the contract: BUNDLE = sys._MEIPASS):
#   provision\root\restore.sh   (~15 KB)
#   provision\root\capture.sh   (~5 KB)
#   provision\root\lib-root.sh  (~3 KB)
#   -> mapped to provision\root\ inside the bundle so cas.provision can read them at
#      BUNDLE\provision\root\*.sh. These are pushed to the device, never user-edited.
#
# WHAT is DELIBERATELY NOT bundled:
#   * data\ (profiles\ writable golden ~3.1 GB; Apps\, retroarch-cores\, ES-DE\, *.apk, *.img)
#                                                                    -> EXTERNAL, beside exe at APPDIR\data\.
#   * platform-tools\ (adb.exe / fastboot.exe)                      -> EXTERNAL, beside exe.
#   * provision\root\firmware\ (stock/patched init_boot.img used by Seal) -> EXTERNAL,
#     resolved off APPDIR via profile.meta stock_init_boot=... . The OPERATOR must drop
#     this dir beside the exe or Seal fails — see scripts\build-win.bat / docs\PACKAGING.md.
#
# tkinter / Tcl-Tk: PyInstaller's bundled hooks (hook-tkinter, the Tk runtime hook)
# collect tkinter, _tkinter, and the Tcl/Tk DLLs + the tcl\ / tk\ data dirs automatically;
# no manual datas/hiddenimports are required for it. GOTCHA for Python 3.14: build with a
# PyInstaller new enough to support 3.14 (>= 6.11). On an older PyInstaller the Tk runtime
# hook can miss the Tcl/Tk data and the GUI dies at launch with "Can't find a usable
# init.tcl" / "tcl/tk library not found". If you hit that, upgrade PyInstaller (don't hand-add
# the tcl dirs). collect_all('tkinter') below is a belt-and-braces safety net.
#
# PyInstaller 6.x API note: bytecode encryption (block_cipher / cipher=) and the
# win_no_prefer_redirects / win_private_assemblies Analysis args were REMOVED in
# PyInstaller 6.0. Passing any of them raises TypeError at spec-eval time, so this spec
# (which targets >= 6.11 for Python 3.14) must NOT reference them.

import sys

import os
from PyInstaller.utils.hooks import collect_all

# This spec lives in scripts/. Anchor every source path to the REPO ROOT (parent of SPECPATH) so the
# build is independent of the current directory when pyinstaller runs — provision/ and assets/ stay at
# the repo root, the entry shims live here in scripts/.
REPO = os.path.dirname(os.path.abspath(SPECPATH))
def _p(*parts):
    return os.path.join(REPO, *parts)

# --- read-only device-side shell scripts -> provision\root\ inside BUNDLE ---
datas = [
    (_p('provision/root/restore.sh'),  'provision/root'),
    (_p('provision/root/capture.sh'),  'provision/root'),
    (_p('provision/root/lib-root.sh'), 'provision/root'),
    (_p('assets/cas-window.png'),      'assets'),          # GameCove logo for the Tk window/taskbar icon
    (_p('assets/app-icons/*.png'),     'assets/app-icons'),  # curated per-app launcher icons for the app list
]

# Belt-and-braces: make sure tkinter's submodules + Tcl/Tk data come along.
# (PyInstaller's stock hooks normally cover this; harmless to be explicit.)
tk_datas, tk_binaries, tk_hidden = collect_all('tkinter')
datas += tk_datas

hiddenimports = list(tk_hidden) + [
    # cas submodules are reached via normal imports from the entry shims, but list the
    # ones loaded indirectly so a future refactor / lazy import can't drop them.
    'cas',
    'cas.adb',
    'cas.cli',
    'cas.gui',
    'cas.profiles',
    'cas.provision',
]

# --- analysis: one per executable; COLLECT dedupes the shared (identical) datas/binaries ---
a_gui = Analysis(
    [_p('scripts/pyi_entry_gui.py')],
    # The entry shim lives in scripts/ but the `cas` package sits at the repo ROOT. PyInstaller
    # only auto-adds the entry script's OWN dir (scripts/) to the analysis import path, so without
    # REPO here `from cas.gui import main` can't resolve and the bundle ships WITHOUT cas ->
    # ModuleNotFoundError: No module named 'cas' at launch (regression from the scripts/ reorg).
    pathex=[REPO],
    binaries=tk_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # trim the bundle: none of these are runtime deps (stdlib-only app).
        'PyInstaller', 'pytest', 'unittest', 'pydoc', 'test',
    ],
    noarchive=False,
)

a_cli = Analysis(
    [_p('scripts/pyi_entry_cli.py')],
    pathex=[REPO],   # see a_gui: repo root must be on the import path so `cas` resolves
    binaries=tk_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PyInstaller', 'pytest', 'unittest', 'pydoc', 'test',
    ],
    noarchive=False,
)

pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data)
pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data)

# --- GUI exe: windowed (no console window pops up on double-click) ---
exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,        # binaries go into COLLECT, not the exe (onedir)
    name='cas-gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # UPX off: it trips SmartScreen/AV more, saves little here
    console=False,                # WINDOWED
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_p('assets/cas-dark.ico'),   # GameCove logo (dark/purple). Swap to assets/cas-light.ico if preferred.
)

# --- CLI exe: console (must show stdout/stderr for scripted/batch runs) ---
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name='cas',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                 # CONSOLE
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_p('assets/cas-dark.ico'),   # same GameCove logo on the CLI exe
)

# --- ONE COLLECT dir holds BOTH exes + the shared runtime/datas, side by side ---
coll = COLLECT(
    exe_gui,
    exe_cli,
    a_gui.binaries, a_gui.zipfiles, a_gui.datas,
    a_cli.binaries, a_cli.zipfiles, a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='cas',                   # -> dist/cas/  (contains cas-gui[.exe] and cas[.exe])
)

# --- macOS only: wrap the COLLECT dir as a double-clickable .app for the GUI ---
#
# BUNDLE(coll, ...) takes the COLLECT output and re-lays it out as CAS.app/Contents/
# (MacOS/ holds BOTH binaries: cas-gui and cas; Resources/ holds the datas). The FIRST
# EXE passed to COLLECT (exe_gui) becomes the bundle's CFBundleExecutable, so double-
# clicking CAS.app launches the windowed GUI. The console `cas` binary is still present
# at CAS.app/Contents/MacOS/cas for scripted use, AND build-macos.sh also copies the
# plain onedir `dist/cas/` so there's a non-.app CLI binary too.
#
# This block is guarded by sys.platform so the SAME spec is reused unchanged on
# Linux/Windows (where BUNDLE is a no-op / not wanted) — it only fires on macOS.
#
# FROZEN-PATH CONTRACT (matches cas/__init__._app_root): for a .app, APPDIR resolves to
# the directory CONTAINING CAS.app (it walks Foo.app/Contents/MacOS/exe up 4 levels).
# So profiles/, platform-tools/, and provision/root/firmware/ MUST be placed BESIDE
# CAS.app (i.e. in dist/), never inside Contents/. build-macos.sh does exactly that.
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='CAS.app',
        icon=_p('assets/cas.icns'),   # GameCove logo for the macOS .app
        bundle_identifier='com.luxium.cas',
        version='0.1.0',
        info_plist={
            'CFBundleName': 'CAS',
            'CFBundleDisplayName': 'CAS — Console Auto Setup',
            'CFBundleShortVersionString': '0.1.0',
            'CFBundleVersion': '0.1.0',
            'NSHighResolutionCapable': True,
            # Tk is not a document-based app; declare no document types.
            'LSMinimumSystemVersion': '11.0',
        },
    )
