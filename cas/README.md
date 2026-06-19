# cas — Console Auto Setup front-end (Python/Tkinter)

A small desktop GUI that is also the **PC-side orchestrator**. The device-side engine
(`provision/root/restore.sh`, `capture.sh`, `lib-root.sh`) is unchanged and runs on the device under
`su`; this package drives adb/fastboot, manages the profile library, and provides the buttons.

## Run it
```
python -m cas                                  # adb/fastboot on PATH
python -m cas --adb windows-kit\adb.exe --fastboot windows-kit\fastboot.exe   # Windows kit
```
or double-click **`run-gui.bat`** (Windows) / **`run-gui.sh`** (Linux/macOS). Needs Python 3 + Tkinter
(both stdlib — no `pip install`).

## What the buttons do
- **Refresh devices** — lists connected units, auto-matches each to a profile by `ro.product.model`.
- **Profile + module checkboxes** — pick a profile; tick which apps/frontend to include → **Save manifest**.
- **Provision selected** — push the manifest's modules from the profile → run `restore.sh` → reboot.
- **Provision ALL (batch)** — every connected unit, each auto-matched to its own profile.
- **Capture / Update golden** — capture the selected golden into a profile (keeps `.prev` for rollback).
- **New / Delete** — create a profile; delete = **archive** to `profiles/_archive` (type-name confirm, never `rm`).
- **Seal (ship-ready)** — run AFTER verifying: disable Developer options → remove Magisk (un-root via
  stock `init_boot`) → disable USB debugging (last; drops adb). Makes the unit retail-locked.

## Layout
```
cas/adb.py        adb + fastboot wrappers (injectable runner -> unit-testable)
cas/profiles.py   profile library: list / match-by-model / manifest parse+save / archive
cas/provision.py  provision / batch / capture-to-pc / seal
cas/gui.py        the Tkinter window
profiles/<name>/  profile.meta + manifest + golden_root_payload/   (the collection)
```
Tests: `python3 tests/test_cas.py` (mock adb/fastboot — no device needed).

## Notes
- SD = bulk game data (ROMs + large PC games); the PC pushes everything else. `su` is `/debug_ramdisk/su`.
- Re-provisioning a sealed unit needs re-enabling USB debugging + re-flashing the patched `init_boot`.
