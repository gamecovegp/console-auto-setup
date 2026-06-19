#!/usr/bin/env bash
#
# build-linux.sh — produce the Linux build of CAS (Console Auto Setup) with PyInstaller.
#
# Output: dist/cas/   (onedir bundle)
#   cas-gui            windowed GUI  (python -m cas)          <- double-click / ./cas-gui
#   cas                console CLI   (python -m cas.cli)      <- ./cas list | provision | ...
#   provision/root/*.sh                                       <- BUNDLED (read-only, pushed to device)
#   _internal/ (or libs alongside)                            <- PyInstaller runtime + Tcl/Tk
# Post-build (staged BESIDE the binaries, in dist/cas/, = the APPDIR the app reads at runtime):
#   profiles/                  <- the writable profile library (golden_root_payload ~7.1 GB)
#   platform-tools/            <- adb + fastboot (auto-detected by cas.find_adb / the GUI shim)
#   provision/root/firmware/   <- per-profile init_boot images that `seal` resolves via APPDIR
#
# ============================================================================
# CANNOT CROSS-COMPILE. PyInstaller freezes the interpreter + native libs of the
# machine it RUNS ON. This script MUST run on Linux and produces a Linux-only
# binary. It will NOT produce a Windows .exe or a macOS .app, and a Windows/macOS
# build will NOT produce this Linux binary. Build each OS on that OS (or a matching
# VM / CI runner: e.g. a Linux GitHub Actions runner).
#
# GLIBC SENSITIVITY. The produced binary is dynamically linked against the build
# host's glibc. A binary built on a NEW distro will fail on OLDER targets with
#   "version `GLIBC_2.XX' not found".
# => Build on the OLDEST glibc / distro you must support (e.g. an old Ubuntu LTS or
#    a manylinux / Debian-oldstable container). Newer targets run an old-glibc build
#    fine; the reverse is not true. glibc is forward-compatible, not backward.
#
# BUILD-TIME SYSTEM LIBS (NOT runtime deps of the app — needed so the frozen Tk works):
#   The app is stdlib-only (tkinter is stdlib), but tkinter needs the system Tcl/Tk
#   libraries present AT BUILD TIME so PyInstaller can collect libtcl*/libtk* and the
#   tcl/ tk/ data dirs. Install the Tk dev libraries for your distro BEFORE building:
#     Debian/Ubuntu : sudo apt-get install -y python3-tk tk-dev tcl-dev
#     Fedora/RHEL   : sudo dnf install -y python3-tkinter tk-devel tcl-devel
#     Arch          : sudo pacman -S --needed tk tcl       (python ships _tkinter)
#   Sanity-check it imports: `python3 -c "import tkinter; tkinter.Tk()"` (needs a DISPLAY,
#   or run headless under xvfb-run just to confirm the libs load).
# ============================================================================
#
# Usage:  ./build-linux.sh
# Env:    PYTHON=python3.14   (override the interpreter used to build)
#         PROFILES_SRC=/path/to/profiles  PLATFORM_TOOLS_SRC=/path/to/platform-tools
#           (override where the external dirs are copied FROM; default: the repo's own)
#
set -euo pipefail

# Resolve repo root = dir of this script (cwd-independent; path may contain spaces/brackets).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"
SPEC="cas.spec"

echo "==> CAS Linux build"
echo "    repo:   $HERE"
echo "    python: $("$PYTHON" --version 2>&1)  ($PYTHON)"

# --- preflight: interpreter + PyInstaller + tkinter -------------------------
if ! "$PYTHON" -c "import sys; raise SystemExit(0 if sys.version_info[:2]>=(3,10) else 1)"; then
  echo "ERROR: need Python >= 3.10 (this app is developed on 3.14). Set PYTHON=…" >&2
  exit 1
fi
if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
  echo "ERROR: PyInstaller not found for $PYTHON." >&2
  echo "       Install it (BUILD-only dep):  $PYTHON -m pip install --upgrade 'pyinstaller>=6.11'" >&2
  echo "       (>=6.11 is required for Python 3.14 support.)" >&2
  exit 1
fi
echo "    pyinstaller: $("$PYTHON" -m PyInstaller --version 2>&1)"
if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
  echo "ERROR: tkinter not importable — install the system Tk libs (see header):" >&2
  echo "       Debian/Ubuntu: sudo apt-get install -y python3-tk tk-dev tcl-dev" >&2
  exit 1
fi

# --- sanity: the unit tests must be green before we freeze anything ---------
echo "==> running unit tests (must pass before building)"
"$PYTHON" -m unittest discover -s tests -q

# --- clean -------------------------------------------------------------------
echo "==> cleaning build/ and dist/"
rm -rf build dist

# --- freeze ------------------------------------------------------------------
echo "==> pyinstaller $SPEC"
"$PYTHON" -m PyInstaller --noconfirm --clean "$SPEC"

DIST="dist/cas"
if [ ! -x "$DIST/cas-gui" ] || [ ! -x "$DIST/cas" ]; then
  echo "ERROR: expected dist/cas/cas-gui and dist/cas/cas — build did not produce them." >&2
  exit 1
fi

# --- verify the read-only device-side scripts got bundled -------------------
# PyInstaller may place datas under dist/cas/ or dist/cas/_internal/ depending on
# version; check both. These are pushed to the device by cas.provision (BUNDLE root).
SCRIPT_OK=0
for base in "$DIST" "$DIST/_internal"; do
  if [ -f "$base/provision/root/restore.sh" ]; then SCRIPT_OK=1; fi
done
if [ "$SCRIPT_OK" -ne 1 ]; then
  echo "ERROR: provision/root/restore.sh was not bundled — check datas in $SPEC." >&2
  exit 1
fi

# --- stage the EXTERNAL APPDIR siblings beside the binaries -----------------
# These are NOT bundled (writable / huge / native tools). At runtime cas resolves
# them via APPDIR = the dir of the executable = dist/cas/.
PROFILES_SRC="${PROFILES_SRC:-$HERE/profiles}"
PLATFORM_TOOLS_SRC="${PLATFORM_TOOLS_SRC:-$HERE/platform-tools}"

echo "==> staging external APPDIR dirs into $DIST/"

# (1) profiles/ — the profile library. Copy if present (the 7.1 GB payload may be
#     intentionally excluded; we still create the dir so the app has a place to write).
if [ -d "$PROFILES_SRC" ]; then
  echo "    profiles/        <- $PROFILES_SRC"
  cp -a "$PROFILES_SRC" "$DIST/profiles"
else
  echo "    profiles/        (source not found; creating empty — drop profiles in later)"
  mkdir -p "$DIST/profiles"
fi

# (2) provision/root/firmware/ — per-profile init_boot images. `seal` reads these via
#     APPDIR/<profile.meta stock_init_boot>, e.g. provision/root/firmware/<fw>/init_boot.img.
#     They live UNDER provision/root/ but are NOT the bundled scripts, so stage them
#     into APPDIR explicitly (alongside, not inside, _internal/).
if [ -d "$HERE/provision/root/firmware" ]; then
  echo "    provision/root/firmware/   <- $HERE/provision/root/firmware"
  mkdir -p "$DIST/provision/root"
  cp -a "$HERE/provision/root/firmware" "$DIST/provision/root/firmware"
else
  echo "    provision/root/firmware/   (none found; seal will need it staged before use)"
fi

# (3) platform-tools/ — adb + fastboot. Auto-detected by cas.find_adb / the GUI shim.
if [ -d "$PLATFORM_TOOLS_SRC" ]; then
  echo "    platform-tools/  <- $PLATFORM_TOOLS_SRC"
  cp -a "$PLATFORM_TOOLS_SRC" "$DIST/platform-tools"
  chmod +x "$DIST/platform-tools/adb" "$DIST/platform-tools/fastboot" 2>/dev/null || true
else
  echo "    platform-tools/  (source not found — install the Linux platform-tools and drop"
  echo "                      adb + fastboot into $DIST/platform-tools/, or rely on PATH)"
  mkdir -p "$DIST/platform-tools"
fi

chmod +x "$DIST/cas-gui" "$DIST/cas"

echo
echo "==> DONE. Linux build at: $DIST/"
echo "    GUI:  ($DIST/) ./cas-gui"
echo "    CLI:  ($DIST/) ./cas list"
echo
echo "    APPDIR layout (everything the app reads is BESIDE the binaries):"
echo "      $DIST/cas-gui      $DIST/cas"
echo "      $DIST/profiles/                   (writable; add golden payloads here)"
echo "      $DIST/platform-tools/             (adb, fastboot)"
echo "      $DIST/provision/root/firmware/    (stock/patched init_boot for Seal)"
echo
echo "    REMINDER: glibc-sensitive — built against $(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1 || echo '?'). "
echo "    For broad compatibility, rebuild on the OLDEST distro you ship to."
echo "    NOTE: this binary runs on Linux ONLY. Build Windows on Windows, macOS on macOS."
