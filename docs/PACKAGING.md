# Packaging CAS — the honest decision

## Do we even need a frozen `.exe`?

**Short answer: only for the Windows operator rig. Everywhere else it's optional.**

CAS is a stdlib-only Python/Tkinter app. Freezing it with PyInstaller buys you exactly
one thing, and it is worth being honest about what that thing is:

- It does **not** remove the dependency on a native `adb` / `fastboot`. Those are
  separate binaries (`platform-tools/`) that ship beside the app no matter how the
  Python is packaged. Freezing Python cannot fold them in usefully.
- It does **not** help the dev/build host — that machine already has Python 3.14 and
  Tk, so `python -m cas` / `python -m cas.cli` is the simplest, fastest path there.
- The **one real win**: it spares the **Windows operator** from installing Python +
  Tcl/Tk and fighting PATH. They double-click `cas-gui.exe`; that's the whole pitch.

So the recommendation is deliberately asymmetric:

| Target | Freeze? | Why |
|--------|---------|-----|
| **Windows** (operator rig, primary) | **Yes** | Operator has no Python/Tk. `cas-gui.exe` is the deliverable. |
| **Linux** (this dev host) | Optional | Python already here; `python -m cas` is simpler. CI builds it anyway for parity / a clean drop-in. |
| **macOS** (future) | Optional | Same as Linux — build it if/when an operator needs it; otherwise run from source. |

### What stays EXTERNAL regardless of freezing

Two things are **never** baked into the executable, on every platform:

- **`profiles/`** — 7.1 GB and **read-write**. `capture` creates new profile dirs;
  archive (delete) **moves** dirs. A writable 3 GB tree cannot live inside a one-file
  blob or a read-only bundle. It is an external sibling of the exe.
- **`platform-tools/`** — `adb` / `fastboot` are native, per-OS binaries, not Python.
  They're vendored beside the exe and auto-detected (`APPDIR/platform-tools/`), with a
  fall back to legacy `windows-kit/` and finally `PATH`. (Per-device `init_boot` firmware
  that `profile.meta` points at via `stock_init_boot=provision/root/firmware/...` is
  resolved off **APPDIR** too, so it also stays external — a firmware drop is a file
  copy, not an app rebuild.)

Only the **three small device-side shell scripts** that the app actually pushes —
`provision/root/{restore,capture,lib-root}.sh` (~22 KB total) — are bundled read-only
into the exe (this is exactly the `datas` list in `scripts/cas.spec`; `cas.provision` reads them
back at `BUNDLE/provision/root/*.sh`). They're pushed to the device and never user-edited.
That is the entire payload of the frozen bundle beyond the Python runtime + Tcl/Tk.
(`provision/root/verify.sh` exists in the source tree but is a manual diagnostic the app
never pushes, so it is deliberately **not** in the bundle.)

This is the **BUNDLE vs APPDIR** split that `cas/__init__.py` encodes:

```
BUNDLE = sys._MEIPASS if frozen else repo ROOT   # read-only: provision/root/*.sh
APPDIR = dir(executable) if frozen else repo ROOT # writable: data/ (profiles, …), platform-tools/
DATA   = APPDIR/"data"                            # all operator runtime data grouped here
```

In source mode `BUNDLE == APPDIR == repo ROOT`, so behavior and the 22 unit tests are
unchanged.

---

## How it's built

PyInstaller **cannot cross-compile** — each OS binary must be built on that OS. So we do
it once, from one push, with a 3-OS CI matrix:

- `.github/workflows/build.yml` — `windows-latest` / `ubuntu-latest` / `macos-latest`,
  each: `checkout` → `setup-python 3.14` (auto-fallback to 3.13 if a runner image can't
  resolve 3.14 — the app is version-agnostic stdlib) → run the unit tests →
  `pip install pyinstaller>=6.11` → `pyinstaller scripts/cas.spec` → `upload-artifact dist/`.
- `scripts/cas.spec` — one **onedir** `COLLECT` that emits **both** executables sharing one
  `_internal/` runtime: `cas-gui` (windowed, `python -m cas`) and `cas` (console,
  `python -m cas.cli`), via the `scripts/pyi_entry_gui.py` / `scripts/pyi_entry_cli.py` shims.
  The spec anchors its `datas` to the repo root via `SPECPATH`, so it builds from any CWD.

> PyInstaller is a **build-only** dependency. The app has **no** third-party runtime
> deps — `tkinter` and `unittest` are stdlib.

**Local Windows build** (instead of pulling the CI artifact): run `scripts\build-win.bat`, which
calls `pyinstaller scripts\cas.spec` and leaves the bundle in `dist\cas\`.

---

## Operator-side folder layout (Windows)

After grabbing the `cas-windows` CI artifact (or running `scripts\build-win.bat`), the operator
drops `data\` (the runtime data) and `platform-tools\` **beside the exe**, inside `dist\cas\`:

```
dist\cas\
├─ cas-gui.exe          # double-click this — the Tkinter GUI (python -m cas)
├─ cas.exe              # headless CLI for batch/scripts (python -m cas.cli)
├─ _internal\           # PyInstaller runtime: Python + Tcl/Tk + bundled provision\root\*.sh
│  └─ provision\root\   #   restore.sh / capture.sh / lib-root.sh  (read-only, the 3 pushed scripts)
├─ data\                # EXTERNAL, read-write — all operator runtime data grouped here
│  ├─ profiles\         #   the 7.1 GB golden library
│  │  └─ odin2mini\
│  │     ├─ profile.meta
│  │     ├─ manifest
│  │     └─ golden_root_payload\     (+ golden_root_payload.prev for rollback)
│  ├─ Apps\             #   emulator/companion APKs (gamecove-companion.apk, Magisk-v30.7.apk, …)
│  ├─ retroarch-cores\  #   curated arm64 RetroArch cores
│  └─ ES-DE\downloaded_media\        #   ES-DE box art pushed per device
└─ platform-tools\      # EXTERNAL — vendored adb/fastboot (auto-detected by both exes)
   ├─ adb.exe
   ├─ fastboot.exe
   └─ *.dll              (AdbWinApi.dll, AdbWinUsbApi.dll, libwinpthread-1.dll)
```

- **The operator workflow IS the app — not scripts.** Launch `cas-gui.exe`, select the
  device(s) (or tick "Apply to ALL connected"), then **⓪ Root** (fresh units) → **② Download**
  (restore the golden + every app + the GameCove Companion) → **③ Lock for shipping**. This
  supersedes the legacy hand-run `windows-kit\*.bat` flow (`1-flash-root` → `2-run-restore` →
  verify), which is kept only as a fallback for benches that can't launch the app.
- `cas-gui.exe` with no args auto-detects `platform-tools\adb.exe` (then legacy
  `windows-kit\`, then `PATH`). `--adb` / `--fastboot` always override.
- `cas.exe` with no flags auto-detects the same sibling `platform-tools\adb.exe` /
  `fastboot.exe` (via `cas.cli`'s `find_adb`), then falls back to `PATH`. `--adb` /
  `--fastboot` override it, e.g.
  `cas.exe --adb platform-tools\adb.exe --fastboot platform-tools\fastboot.exe provision-all`.
  (Note: unlike `cas-gui.exe`, the CLI's auto-detect does **not** probe legacy `windows-kit\`.)
- Updating the golden = replacing/adding dirs under `data\profiles\`. Updating firmware =
  dropping images under `provision\root\firmware\` next to the exe (APPDIR). Neither
  requires rebuilding the exe.

On **Linux/macOS** the layout is identical (`cas-gui` / `cas` with no `.exe`, `platform-tools/`
holding the platform's `adb`/`fastboot`); on macOS the `.app`-aware path logic in
`cas/__init__.py` keeps `data/` and `platform-tools/` beside the `.app`, not inside it.

## Updating CAS itself — releases + in-app self-update

Operators don't run `update-win.bat` (git pull + PyInstaller rebuild) anymore. The app
**self-updates from the GitHub Release**, so a bench needs no git / Python / PyInstaller —
just the app and its sibling folders.

**To ship an update — push a tag** (the version comes FROM the tag; nothing to bump in source):

```bash
git tag v0.2.0 && git push origin v0.2.0
```

`.github/workflows/build.yml` then, on a `v*` tag: injects `__version__` from the tag, builds
`dist/cas` on a Windows/Linux/macOS runner each, zips + sha256s every bundle, assembles
`latest.json`, and publishes a **GitHub Release** with the three `cas-<os>.zip`s + `latest.json`.

**What the app does:** on startup (and via **Help → Check for updates…**) it reads
`https://github.com/<repo>/releases/latest/download/latest.json` (public repo → no auth),
compares `cas/__init__.py`'s `__version__`, and if a newer build exists prompts to download.
On accept it downloads this OS's zip, **sha256-verifies** it, then a small helper waits for the
app to exit, **overwrite-copies** (`robocopy /E`, never `/MIR`) the new bundle over `dist/cas`,
and relaunches. The external siblings (`data/` — profiles, Apps, ES-DE, cores — plus
`platform-tools/` and `provision/root/firmware/`) live OUTSIDE the bundle, so the swap never touches them.

**Bootstrap (one-time):** the updater is in the code, so any build from `main` has it.
Distribute ONE build to each bench (the app + its sibling folders). After that, tags drive
updates automatically — existing installs report `0.1.0`, so the first `v0.2.x` tag prompts
everyone. The in-app check logic is unit-tested (`TestUpdater`); the per-OS swap+relaunch helper
should be smoke-tested on a real Windows bench before relying on unattended updates.
