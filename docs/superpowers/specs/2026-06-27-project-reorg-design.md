# Project directory reorg — design & migration map (2026-06-27)

Goal: group the flat top-level into `cas/ tests/ provision/ assets/ scripts/ docs/ data/`, keeping the
frozen-path contract intact. Approved scope: **full** (data dirs + scripts/ + assets/branding), build
toolchain moved into `scripts/`, applied on top of existing WIP **without committing**.

## Target top-level
```
cas/  tests/  provision/            # unchanged (provision/ bundled via cas.spec)
assets/{app-icons/, branding/, *.ico *.icns *.png}
scripts/{lib/ modes/ results/  run* cas.bat uiauto.sh fastboot-check.bat
         build-*.{sh,bat} update*.{sh,bat}  cas.spec pyi_entry_*.py}
docs/{superpowers/…, TESTING.md PACKAGING.md NEXT-STEPS.md}
data/{profiles/ Apps/ ES-DE/ retroarch-cores/ Bios/ payloads/ downloaded_media/ device-firmwares/}
README.md  cas-config.json  platform-tools/  .gitignore .gitattributes .github/ .superpowers/
```
`platform-tools/`, `provision/root/firmware/`, `cas-config.json` stay at root (code reads them via
`APPDIR/…`). `README.md` stays at root (convention).

## Frozen-path mechanic
New constant in `cas/__init__.py`: `DATA = APPDIR / "data"` (dev: `repo/data`; frozen: `<beside-exe>/data`).
All PC-side data refs hang off it, so dev + frozen shift in lockstep.

## Moves
- `branding/` → `assets/branding/`  (mv; untracked)
- 8 dirs → `data/`: profiles Apps ES-DE retroarch-cores Bios payloads downloaded_media device-firmwares (mv; untracked)
- tracked → `scripts/` (git mv): lib/ modes/ run.sh run-gui.sh run.ps1 run.bat run-gui.bat cas.bat
  uiauto.sh fastboot-check.bat build-linux.sh build-macos.sh build-win.bat update.sh update-win.bat
  cas.spec pyi_entry_cli.py pyi_entry_gui.py
- `results/` → `scripts/results/` (mv; untracked — co-located with its generator run.ps1/run.sh)
- tracked → `docs/` (git mv): TESTING.md PACKAGING.md NEXT-STEPS.md

## Edits
| File | Change |
|------|--------|
| `cas/__init__.py` | add `DATA = APPDIR / "data"`; note in path-model docstring |
| `cas/provision.py` | import DATA; `CORES_SRC`/`MEDIA_SRC`/`COMPANION_SRC` → `DATA/…`; `DEFAULT_MAGISK_APK="data/Apps/Magisk-v30.7.apk"` |
| `cas/config.py` | local fallback `APPDIR/"profiles"` → `APPDIR/"data"/"profiles"` (+ docstring) |
| `tests/test_cas.py` | `APPDIR/"profiles"` assertions → `APPDIR/"data"/"profiles"` (lib fallback test) |
| `.gitignore` | drop the 8 per-dir lines + `/branding/` + `/results/`; add `/data/`, `/assets/branding/`, `/scripts/results/` |
| `scripts/cas.spec` | entry scripts → `scripts/pyi_entry_*.py`; update external-dir comments to `data\…` (datas/icons unchanged — relative to repo-root build CWD) |
| `scripts/build-linux.sh`, `build-macos.sh` | `HERE`→repo root (`…/..`); `SPEC=scripts/cas.spec`; `PROFILES_SRC=$HERE/data/profiles`; stage `$DIST/data/profiles` (+ `mkdir -p $DIST/data`) |
| `scripts/build-win.bat` | `cd /d "%~dp0.."`; `PyInstaller … scripts\cas.spec` |
| `scripts/update.sh` | `cd …/..`; call `scripts/build-*.sh`; `link data/{retroarch-cores,profiles,Apps}`, `link data/ES-DE/downloaded_media` |
| `scripts/update-win.bat` | `cd /d "%~dp0.."`; `call scripts\build-win.bat`; junctions into `dist\cas\data\…` |
| `scripts/cas.bat`, `run-gui.bat` | `cd /d "%~dp0.."` (windows-kit + `python -m cas` resolve from repo root) |
| `scripts/run-gui.sh` | `cd "$(dirname "$0")/.."` |
| `scripts/run.sh` | none — sources `$DIR/lib`+`$DIR/modes` which moved alongside |
| `scripts/run.ps1` | outward probes only: `$root\platform-tools`→`$root\..\platform-tools`; `..\odin-provisioning`→`..\..\odin-provisioning` (`$root\lib`, `results\` resolve as-is) |
| `scripts/fastboot-check.bat` | `%~dp0platform-tools`→`%~dp0..\platform-tools`; odin-provisioning probe `+..\` |
| docs cross-links | fix any README/build refs to moved `TESTING.md`/`PACKAGING.md` |

## Verification
- **Can verify here:** `pytest`/unittest (all Python path changes), `git check-ignore data/`, static grep for stale top-level path refs.
- **Operator must verify (can't run here):** a PyInstaller build (`build-*`/`update-*`/`cas.spec`) + a one-device frozen smoke test. The Windows/macOS/PowerShell launchers are static-reviewed only.

## Out of scope / notes
- `recipes/` is empty (WIP-deleted); run.ps1's `$root\recipes` mode stays WIP-broken, not addressed here.
- Device-side paths in docs/provision scripts (e.g. `/storage/…/ES-DE`) are NOT PC paths — left untouched.
- Profile.meta overrides that hard-code `Apps/…` (in gitignored profiles) would need `data/Apps/…`; edge case, flagged not auto-fixed.
