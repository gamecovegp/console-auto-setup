r"""Frozen CLI entry point (cas.exe). Mirrors `python -m cas.cli`.

PyInstaller freezes a *script*, not a `-m package` invocation, so this thin shim
delegates straight to cas.cli.main(), which already does its own argparse
(list | provision | provision-all | capture | seal, with --adb/--fastboot/--serial/--profile).

Auto-detection note: with no --adb/--fastboot flag, cas.cli auto-detects a sibling
APPDIR/platform-tools/<tool>[.exe] (via cas.find_adb) and falls back to PATH — same as
the GUI, except the CLI does NOT also probe the legacy windows-kit\ dir. An explicit
--adb/--fastboot always overrides, e.g.
  cas.exe --adb platform-tools\adb.exe --fastboot platform-tools\fastboot.exe <cmd>
"""
import sys

from cas.cli import main

if __name__ == "__main__":
    sys.exit(main())
