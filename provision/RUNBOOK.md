# RUNBOOK — fresh device → fully provisioned → sealed (on-device, via Shizuku)

End-to-end flow for provisioning a **freshly factory-reset unit** with no PC, using Shizuku + Termux to
run the scripts on-device. Same scripts also run over USB-ADB from a PC (`MODE=adb`) — see README.md.

## What's on the SD card (prepared once from the golden)
```
/storage/<sd>/
  ROMs/<system>/…           games (per system: gc, switch, psx, ps2, nds, n64, psp, dreamcast, snes, …)
  ES-DE/…                   frontend config (settings, custom_systems, gamelists) — rides the card
  Bios/…                    BIOS the emulators import (switch firmware zip, dc, ds, …)
  apks/                     emulator APKs captured from the golden (eden, retroarch, dolphin, …)
  golden_payload/           read-only assets to clone (eden keys/firmware/driver, dolphin Config, *bios, retroarch.cfg, citra-emu)
  provision/                THIS toolkit (master.sh, lib.sh, apks.sh, cleanup.sh, emulators/, uiauto.sh)
  Shizuku.apk  Termux.apk   the two bootstrap installers
```

## STEP 1 — bootstrap (manual, ~3 min, the only hands-on part)
1. Factory-reset unit → finish wizard → **Developer options → USB debugging + Wireless debugging ON** (Wi-Fi on).
2. Install **Shizuku.apk** and **Termux.apk** from the SD (Files app → tap each → allow install).
3. **Start Shizuku** (no PC): Shizuku → *Pairing* → in Wireless debugging "Pair device with pairing code" →
   enter code → *Start*.  (PC alternative: `adb shell <shizuku>/lib/arm64/libshizuku.so`.)
   → confirm "Shizuku is running".
4. **Set up rish:** Shizuku → ⋮ → "Use Shizuku in terminal apps" → copy `rish` + `rish_shizuku.dex` into
   Termux `~/`. In Termux: `chmod +x rish && ./rish` → tap **Allow** on the Shizuku prompt → `id` shows `uid=2000(shell)`.

## STEP 2 — run the setup (automated, inside the rish shell)
```
cd /storage/<sd>/provision
CAS_MODE=local sh master.sh
```
This runs, in order:
- **install APKs** (`apks.sh`) — `pm install` every emulator from `apks/` (fresh device).
- **provision each emulator** — assets cloned, perms granted (`pm grant`/`appops`), SAF folders granted
  via the `uiauto` macro (Dolphin/DuckStation/NetherSX2/melonDS), settings applied (RetroArch overlay-off
  + ROM dir; Dolphin buttons/graphics via Config; Citra config).
(`sh master.sh`, not `./`, because the SD is `noexec`.)

## STEP 3 — finish the GL-UI exceptions (manual taps Shizuku can't do)
OpenGL-rendered UIs ignore synthetic input, so on the device:
- **RetroArch** → Online Updater → Core Downloader → download the cores you ship (gates GC/Wii/3DS/DS + all retro).
- **PPSSPP** → confirm the memstick "OK" (script set it to ROMs/psp via the system picker).
- **melonDS** → point DS BIOS at the SD `Bios` (or enable FreeBIOS); **Dolphin** → add the `wii` folder (2nd grant).

## STEP 4 — seal for retail (automated)
```
CAS_MODE=local SEAL=1 sh master.sh          # or:  CAS_MODE=local sh cleanup.sh
```
`cleanup.sh` deletes `provision/`, `golden_payload/`, `apks/`, backups from the SD, then uninstalls
**Termux** and (last) **Shizuku** — ending the rish session. KEEPS ROMs, ES-DE, Bios, saves, emulators.
Then **reboot** and verify: games boot from ES-DE, no provisioning apps remain, Shizuku gone.

## One-liner per unit (after bootstrap)
```
CAS_MODE=local SEAL=1 sh /storage/<sd>/provision/master.sh
```
→ install APKs → provision all → seal. (Do STEP 3 taps before sealing.)

## Notes / limits
- Shizuku = shell-uid, **not root**; stops on reboot (re-Start per boot). It drives standard-Android UIs
  + system dialogs but **cannot** drive OpenGL UIs (PPSSPP/RetroArch) → those stay manual.
- `/data/data`-only settings (DuckStation fast-boot/controller, NetherSX2 renderer, M64Plus controls) can't
  be pushed — set on the golden; they default acceptably for ES-DE launching.
- For **retail cleanliness**, the **USB-ADB** route (PC) leaves nothing installed and may be preferred at a bench.
