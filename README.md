# console-auto-setup — Odin emulator setup + storage-mapping diagnostics

A debug-first, **Windows** toolkit to (1) find a method that makes the **emulator → ROM-folder
mapping** stick and clone, and (2) set each emulator up **correctly** end-to-end (keys, firmware,
GPU driver, settings) — so a unit + its SD card is **playable the moment it's plugged in**.

---

## 📍 Current status — Odin2 Mini (from real test runs)

| Fact | Result |
|---|---|
| Device | **Odin2 Mini**, Adreno 740, **Android 13**, SD `9C33-6BBD` |
| Method A — ADB file access | ✅ **rw** (file methods work) |
| Method D — adb backup/restore | ❓ **not properly tested yet** — a 0-byte file appeared, but that was likely an incomplete attempt (you must tap **"Back up my data"** on the Odin). Re-test before ruling it out. |
| Method B — root (`adb root`) | ❌ **NO** (see `docs/TESTING.md`) — closed unless the bootloader gets unlocked (wipes device) |
| Method E — Shizuku | **CLOSED** — server runs but is shell-capped → adds nothing over USB. Tooling + notes archived 2026-06-11 |
| TEST 1c — `clonetest flycast` | ⚠️ 2026-06-11 run **VOID**: the push-back silently failed on Android 13 (device `emu.cfg` = 0 bytes afterwards) → "games + settings gone" proved nothing. Script now primes the app + **byte-verifies** the push. **Re-run; judge only after `[ok] clone verified`; launch from ES-DE, not Flycast itself** |
| Mapping type per emulator | RetroArch = **PLAIN** ✅ · Dolphin / Flycast / Eden = **SAF** · m64plus = **/data/data** · DuckStation/Citra/melonDS/AetherSX2/PPSSPP = **not set up yet** |

**The crux:** file access works, but the big emulators map games via **SAF `content://`** links whose
*permission* can't be copied — that's why plain file-restore left empty game lists. With **adb backup
dead**, the two no-touch paths to plug-and-play are: **root** (clone the SAF grants) **or** convert
those emulators to **plain paths + All-Files-Access** (if they accept it). The two open tests
(`root-check`, and the Flycast plain-path test) decide which — see `docs/TESTING.md`.

---

# ▶ WINDOWS — STEP BY STEP

Use **`docs/TESTING.md`** as the printable checklist + results form. Steps 1–4 are read-only (safe); step 5 writes to the device.

## STEP 0 — one-time prep
1. **Keep this folder next to `odin-provisioning`** (both in `[07] Projects\`) — the tools borrow
   `adb` (and `fastboot`) from `odin-provisioning\platform-tools\`. Otherwise put them on PATH.
2. **On the Odin:** Settings → About → tap **Build number** 7× → **Developer options** → **USB debugging** ON.
3. **Plug in via USB**, tap **Allow** on the Odin.
4. **One-time PowerShell unlock** (only `run.bat` needs it; `test-methods.bat` is plain batch):
   ```
   powershell -Command "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
   ```
   Copied from a USB stick and Windows "blocked" it? Also run once, from this folder:
   ```
   powershell -Command "Get-ChildItem '*.ps1' | Unblock-File"
   ```

> **Open a command prompt here:** in File Explorer, click the address bar, type `cmd`, Enter.

## STEP 1 — which access methods work? ✅ DECIDED — skip
A = **rw** · root = **NO** · adb backup = unconfirmed/optional (m64plus only) · Shizuku = **closed**.
The matrix tool + Shizuku notes are archived in `_archive\projects\console-auto-setup\` (Work root).

## STEP 1b — (only if you need root and `adb root` says no) `fastboot-check.bat`
Reboots to the bootloader, reads whether it's **unlocked**, reboots back. It does **not** flash anything.
> ⚠️ **If the Odin gets stuck on the bootloader menu** (Start / Restart bootloader / Recovery / Emergency):
> use the **volume buttons** to highlight **`Start`** and press **power** — that boots normally back to
> Android. (Power-hold ~10s also forces a reboot. Never pick Recovery/Emergency.)

## STEP 2 — how does each emulator map its games? `run.bat inspect`
Prints a **TYPE** per emulator (PLAIN / SAF / DATA-DATA / NO CONFIG) + the method to use. Saved to `results\`.

## STEP 3 — read real settings `run.bat getcfg eden` / `getcfg retroarch`
Dumps the config so we pin exact keys. 👉 paste lines with `overlay`, `driver`, or a `/storage/` path.

## STEP 4 — full setup status `run.bat checklist eden` / `checklist retroarch`
PASS / MISSING / WRONG per setup item (keys, firmware, driver, overlay, mapping) with the fix.

## STEP 5 — apply fixes (writes to device; only once we agree the method)
```
run.bat grant   retroarch
run.bat setpath retroarch input_overlay_opacity "0.000000"
run.bat setpath retroarch rgui_browser_directory "/storage/9C33-6BBD/ROMs"
```
Test whether a SAF emulator accepts a plain path (the "do we even need root?" test):
```
run.bat grant   flycast
run.bat setpath flycast Dreamcast.ContentPath "/storage/9C33-6BBD/ROMs/dreamcast"
```
…then open the emulator on the Odin and check the game list. Re-run `run.bat checklist <emu>` to confirm `PASS`.

## 📨 What to send me
Work through **`docs/NEXT-STEPS.md`** (the single forward doc) and bring back its filled results form
plus anything new in `results\`. From that I lock the per-emulator recipes and we're in production.

---
---

# Reference

## What the verdicts mean (updated from real results)
| Result | What it means for plug-and-play |
|---|---|
| **A = rw** ✅ (confirmed) | file-clone works for PLAIN configs + for carrying keys/firmware/drivers/BIOS |
| **D — needs a proper test** | the 0-byte file was an incomplete run; tap "Back up my data" on the device, then check the `.ab` size. Note: even when it works, adb backup carries an app's `/data/data` settings but **not** SAF folder grants (those live in `/data/system`). |
| **B = YES** (if root works) | clone the SAF grants + reach `/data/data` → fully identical, zero-touch. Best case. |
| **B = NO, but plain paths work** | convert SAF emulators to plain path + All-Files-Access → still zero-touch, no root |
| **B = NO and plain doesn't work** | RetroArch (PLAIN) clones the bulk; Dolphin/Flycast/Eden need a ~10s folder re-pick per unit |
| **Shizuku (closed)** | shell-capped → nothing beyond Method A; only relevant if we ever want no-PC on-device setup |

## Why plain file-restore didn't map
The big emulators store the game folder as a SAF `content://` URI whose **permission grant** lives in
`/data/system` (root-only). Copying config files carries the *link* but not the *permission* → empty
game list. `inspect` flags which emulators are affected.

## Modes — `run.bat <mode> [args]`  (Linux: `./run.sh <mode>`)
| Mode | Does | Writes? |
|---|---|---|
| `inspect [emu]` | classify each emulator's mapping + recommend a method | read-only |
| `checklist <emu>` | full per-emulator setup status | read-only |
| `getcfg <emu>` | dump the emulator's config file(s) | read-only |
| `root-check` | temp-root + `/data/data` reachability | read-only |
| `setpath <emu> <key> <value>` | write a config key (Method A); keeps a `.orig` backup | config |
| `grant <emu>` | grant All-Files-Access (Method A) | app-op |
| `backup <emu>` / `restore <emu>` | adb backup/restore (Method D — **tap "Back up my data" on the device**; check the `.ab` size to confirm it works) | app data |

## Per-emulator recipes (`recipes\<emu>.txt`)
Plain data (`label ; kind ; target ; hint`), shared by the Windows + Linux engines.
Captured: **Eden** (keys → firmware → GPU driver → disable overlay → games folder), **RetroArch**
(download all cores → overlay opacity 0 → ROM dir) and **Flycast** (games folder → Vulkan →
virtual-gamepad transparency → mappings → All-Files). On the golden Odin so far: Eden's **prod.keys,
firmware (238), and Turnip driver = PASS**; RetroArch **cores still MISSING**. Flycast's golden
capture (2026-06-11) still showed `pvr.rend = 0` (OpenGL) + `VirtualGamepadTransparency = 100` —
the Vulkan + transparency values were set in-app *after* that capture, so the next
`clonetest`/`getcfg flycast` pins the exact values for `setpath`.
> Eden's "disable overlay" toggle is **not** in `config.ini` (it's an in-app/Android-prefs setting) —
> tell me what it's called in Eden's menu and I'll wire it.

## Files
```
console-auto-setup\
  cas\                 the PC-side app (GUI + CLI) — `python -m cas`
  provision\           device-side shell engine (bundled into the build)
  assets\              app icons + branding source
  docs\
    NEXT-STEPS.md      ▶ START HERE — current state + exactly what to do next
    TESTING.md         test protocol + results form (reference)
    PACKAGING.md       build/freeze + operator drop-in layout
  scripts\             dev/build tooling (grouped)
    fastboot-check.bat bootloader/root-path check (last-resort fallback only)
    run.bat / run.ps1  Windows engine (PowerShell)        run.sh  Linux/Mac engine
    lib\emulators.txt  emulator registry (shared data)
    build-*.sh / build-win.bat   freeze with PyInstaller (scripts\cas.spec)
    results\           timestamped reports + config backups (never overwritten)
  data\                operator-supplied runtime data (profiles, Apps, ES-DE, … — not in git)
```

## Linux/Mac
Same logic, same data files: `./run.sh inspect`, `./run.sh checklist eden`, etc.
