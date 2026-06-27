#!/usr/bin/env bash
#
# build-macos.sh — produce the macOS build of CAS (Console Auto Setup) with PyInstaller.
#
# Outputs (in dist/):
#   CAS.app                    <- the windowed GUI, double-clickable  (from BUNDLE() in cas.spec)
#                                 Contents/MacOS/cas-gui  (CFBundleExecutable)
#                                 Contents/MacOS/cas      (the CLI is ALSO inside the app bundle)
#   cas/                       <- the plain onedir bundle (a NON-.app CLI binary for scripting):
#                                 cas/cas       console CLI   (python -m cas.cli)
#                                 cas/cas-gui   GUI            (same binary, sans .app wrapper)
# Post-build, staged BESIDE CAS.app (in dist/, = the APPDIR a .app resolves at runtime):
#   profiles/                  <- writable profile library (golden_root_payload ~7.1 GB)
#   platform-tools/            <- adb + fastboot (macOS builds)
#   provision/root/firmware/   <- per-profile init_boot images that `seal` resolves via APPDIR
#
# ============================================================================
# CANNOT CROSS-COMPILE. This MUST run on macOS and produces a macOS-only build.
# It will NOT produce a Linux ELF or a Windows .exe, and Linux/Windows builds will
# NOT produce this .app. Build each OS on that OS (or a matching VM / CI runner,
# e.g. a macos-* GitHub Actions runner). Also note ARCHITECTURE: a build on Apple
# Silicon yields an arm64 binary; on Intel, x86_64. For a universal2 build you need
# a universal2 Python and target_arch='universal2' in the spec — out of scope here;
# build on the arch you ship, or use a CI matrix.
# ============================================================================
#
# macOS GOTCHAS this script handles:
#
#  1) FROZEN-PATH for a .app. cas/__init__._app_root() detects when the executable
#     lives at  CAS.app/Contents/MacOS/<exe>  and walks UP to the directory CONTAINING
#     CAS.app. So profiles/, platform-tools/, and provision/root/firmware/ MUST sit
#     BESIDE CAS.app (in dist/), NEVER inside Contents/. This script stages them there.
#     (This is the macOS handling the refactor was REQUIRED to implement — verified
#     present in cas/__init__.py: the parts[-2]=="MacOS" / parts[-4].endswith(".app")
#     special case. If that code is missing, a .app GUI cannot find its profiles.)
#
#  2) CODE SIGNING. PyInstaller already ad-hoc signs the .app at BUNDLE time. Recent
#     macOS also invalidates the bundled Python shared library's signature during the
#     partial framework copy; the fix is to (ad-hoc) RE-SIGN the whole bundle, which we
#     do below with `codesign --deep --force -s -`. ("-s -" = ad-hoc / no identity.)
#     For a notarizable, Gatekeeper-clean distributable you need a paid Developer ID
#     cert + notarization; set CODESIGN_IDENTITY="Developer ID Application: …" to use it.
#
#  3) GATEKEEPER QUARANTINE. An UNSIGNED (ad-hoc) app downloaded via a browser/AirDrop
#     gets the com.apple.quarantine xattr and macOS blocks it ("can't be opened, …
#     unidentified developer"). The RECIPIENT removes it with:
#         xattr -dr com.apple.quarantine /path/to/CAS.app
#     We strip it on the freshly built artifacts here too (a local build usually has no
#     quarantine, but this makes a zipped-and-reopened copy behave).
#
# Usage:  ./build-macos.sh
# Env:    PYTHON=python3.14
#         CODESIGN_IDENTITY="Developer ID Application: Foo (TEAMID)"   (default: ad-hoc "-")
#         PROFILES_SRC=… PLATFORM_TOOLS_SRC=…   (override external-dir sources)
#
set -euo pipefail

# Repo root = PARENT of this script's dir (script lives in scripts/). Build runs from repo root so
# cas.spec's relative datas (provision/, assets/) and the staged external dirs (data/) resolve.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"
SPEC="scripts/cas.spec"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"   # "-" = ad-hoc

echo "==> CAS macOS build"
echo "    repo:   $HERE"
echo "    python: $("$PYTHON" --version 2>&1)  ($PYTHON)"
echo "    arch:   $(uname -m)"
echo "    sign:   ${CODESIGN_IDENTITY} $( [ "$CODESIGN_IDENTITY" = "-" ] && echo '(ad-hoc)' )"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "ERROR: this script must run on macOS (uname=$(uname -s)). Cross-compilation is impossible." >&2
  exit 1
fi

# --- preflight ---------------------------------------------------------------
if ! "$PYTHON" -c "import sys; raise SystemExit(0 if sys.version_info[:2]>=(3,10) else 1)"; then
  echo "ERROR: need Python >= 3.10 (developed on 3.14). Set PYTHON=…" >&2
  exit 1
fi
if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
  echo "ERROR: PyInstaller not found for $PYTHON." >&2
  echo "       Install it (BUILD-only dep):  $PYTHON -m pip install --upgrade 'pyinstaller>=6.11'" >&2
  exit 1
fi
echo "    pyinstaller: $("$PYTHON" -m PyInstaller --version 2>&1)"
# tkinter on macOS: the python.org installers ship Tcl/Tk; Homebrew python needs
# `brew install python-tk@<ver>`; the system /usr/bin/python3 Tk is deprecated/old.
if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
  echo "ERROR: tkinter not importable. Use a python.org build, or for Homebrew python run:" >&2
  echo "       brew install python-tk@3.14   (match your python version)" >&2
  exit 1
fi

# --- tests -------------------------------------------------------------------
echo "==> running unit tests (must pass before building)"
"$PYTHON" -m unittest discover -s tests -q

# --- clean -------------------------------------------------------------------
echo "==> cleaning build/ and dist/"
rm -rf build dist

# --- freeze (the spec's BUNDLE() block fires only on darwin -> emits CAS.app) -
echo "==> pyinstaller $SPEC"
"$PYTHON" -m PyInstaller --noconfirm --clean "$SPEC"

APP="dist/CAS.app"
ONEDIR="dist/cas"
if [ ! -d "$APP" ]; then
  echo "ERROR: $APP not produced — the BUNDLE() block in $SPEC must run on macOS." >&2
  echo "       Confirm the spec has the 'if sys.platform == \"darwin\": app = BUNDLE(coll, …)' block." >&2
  exit 1
fi
if [ ! -x "$APP/Contents/MacOS/cas-gui" ]; then
  echo "ERROR: $APP/Contents/MacOS/cas-gui missing — GUI is not the bundle executable." >&2
  exit 1
fi

# --- re-sign: fix the invalidated bundled-Python signature (gotcha #2) -------
# --deep signs nested binaries (cas, cas-gui, Tcl/Tk, libpython). --force overwrites
# the ad-hoc sig PyInstaller already applied. "-s -" keeps it ad-hoc unless an identity
# was provided. This must come AFTER any modification to the bundle contents.
echo "==> codesign (--deep --force -s '$CODESIGN_IDENTITY')"
codesign --deep --force --options runtime -s "$CODESIGN_IDENTITY" "$APP" \
  || codesign --deep --force -s "$CODESIGN_IDENTITY" "$APP"
codesign --verify --deep --verbose=2 "$APP" || {
  echo "WARNING: codesign --verify reported issues (expected to still RUN for an ad-hoc/local build)." >&2
}

# --- strip Gatekeeper quarantine on the built artifacts (gotcha #3) ----------
echo "==> xattr -dr com.apple.quarantine (clears Gatekeeper quarantine)"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
xattr -dr com.apple.quarantine "$ONEDIR" 2>/dev/null || true

# --- stage the EXTERNAL APPDIR siblings BESIDE CAS.app (gotcha #1) ----------
# For a .app, APPDIR = the dir CONTAINING the .app = dist/. Place the writable/native/
# firmware trees there so both CAS.app AND the standalone dist/cas/ CLI resolve them.
PROFILES_SRC="${PROFILES_SRC:-$HERE/data/profiles}"
PLATFORM_TOOLS_SRC="${PLATFORM_TOOLS_SRC:-$HERE/platform-tools}"
DESTS=("dist")          # beside CAS.app
# Also mirror into the onedir so dist/cas/ works as a self-contained CLI drop too.
[ -d "$ONEDIR" ] && DESTS+=("$ONEDIR")

for D in "${DESTS[@]}"; do
  echo "==> staging external APPDIR dirs into $D/"
  mkdir -p "$D/data"   # the app reads runtime data via APPDIR/data/
  # data/profiles/
  if [ -d "$PROFILES_SRC" ]; then
    echo "    data/profiles/   <- $PROFILES_SRC"
    rm -rf "$D/data/profiles"; cp -a "$PROFILES_SRC" "$D/data/profiles"
  else
    echo "    data/profiles/   (source not found; creating empty)"
    mkdir -p "$D/data/profiles"
  fi
  # provision/root/firmware/  (seal reads APPDIR/<stock_init_boot>)
  if [ -d "$HERE/provision/root/firmware" ]; then
    echo "    provision/root/firmware/   <- $HERE/provision/root/firmware"
    mkdir -p "$D/provision/root"
    rm -rf "$D/provision/root/firmware"; cp -a "$HERE/provision/root/firmware" "$D/provision/root/firmware"
  else
    echo "    provision/root/firmware/   (none found; stage before using Seal)"
  fi
  # platform-tools/  (adb + fastboot)
  if [ -d "$PLATFORM_TOOLS_SRC" ]; then
    echo "    platform-tools/  <- $PLATFORM_TOOLS_SRC"
    rm -rf "$D/platform-tools"; cp -a "$PLATFORM_TOOLS_SRC" "$D/platform-tools"
    chmod +x "$D/platform-tools/adb" "$D/platform-tools/fastboot" 2>/dev/null || true
    # adb/fastboot are themselves unsigned native bins -> clear their quarantine too.
    xattr -dr com.apple.quarantine "$D/platform-tools" 2>/dev/null || true
  else
    echo "    platform-tools/  (source not found — install macOS platform-tools and drop"
    echo "                      adb + fastboot into $D/platform-tools/, or rely on PATH)"
    mkdir -p "$D/platform-tools"
  fi
done

echo
echo "==> DONE. macOS build in dist/:"
echo "    GUI app:  open dist/CAS.app          (double-clickable; APPDIR = dist/)"
echo "    CLI:      dist/cas/cas list          (standalone onedir CLI)"
echo "         or:  dist/CAS.app/Contents/MacOS/cas list   (CLI inside the bundle)"
echo
echo "    APPDIR layout (BESIDE CAS.app, NOT inside Contents/):"
echo "      dist/CAS.app"
echo "      dist/profiles/                   (writable; add golden payloads here)"
echo "      dist/platform-tools/             (adb, fastboot)"
echo "      dist/provision/root/firmware/    (stock/patched init_boot for Seal)"
echo
echo "    DISTRIBUTING UNSIGNED: the recipient must clear Gatekeeper quarantine once:"
echo "        xattr -dr com.apple.quarantine /Applications/CAS.app"
echo "    For a notarized build, re-run with CODESIGN_IDENTITY='Developer ID Application: …'"
echo "    then notarize (xcrun notarytool submit … --wait) and staple (xcrun stapler staple)."
echo
echo "    NOTE: this build runs on macOS $(uname -m) ONLY. Build Windows on Windows, Linux on Linux."
