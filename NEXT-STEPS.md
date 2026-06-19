# NEXT STEPS — GameCove handheld provisioning (updated 2026-06-11)

The single forward doc. Everything already decided is listed once; everything left is a numbered
step. Deep detail lives in: `..\odin-provisioning\START-HERE.md` (production flow),
`GOLDEN-SETUP.md` (Odin golden spec), `MANGMI-SETUP.md` (Mangmi runbook), `TESTING.md` (reference).

## ✅ Decided — do not retest
| Question | Answer |
|---|---|
| File access (Method A) | **rw** — the backbone; clones configs, keys, firmware, BIOS, cores |
| Root (B/B′) | **NO** — bootloader locked; last resort only (`fastboot-check.bat` if it ever comes to that) |
| adb backup (D) | unconfirmed, optional — only matters for M64Plus FZ; 2-min check folded into step 1 |
| Shizuku (E) | **closed** — shell-capped, adds nothing over USB; tooling archived |
| Frontend | **ES-DE** on Odin · "Game Launcher" on Mangmi |
| Clone engine | golden capture → ADB file clone (`odin-provisioning` toolkit) + `seal-unit.bat` before boxing |
| Per-emu map (Odin) | RetroArch **PLAIN** ✅ · Dolphin/Flycast/Eden **SAF** (step 2 decides) · m64plus /data/data |

## ⚠️ Open — what the next session must answer
1. **Does ES-DE launch SAF emulators on a cloned unit?** → zero-config, or +10s folder pick per unit.
2. **Does the full loop hold end-to-end?** capture → factory reset → provision → PASS → games boot.
3. **Mangmi:** access level (rw?), real package list, golden built per the runbook.

---

# ▶ THE SESSION — run in this order

## 0 · Finish the golden first (one known gap)
- **RetroArch cores = MISSING** (checklist 2026-06-11). On the golden: Online Updater →
  Core Downloader → download the ship set. **Capturing before this ships every unit coreless.**
- Re-check: `run.bat checklist retroarch` / `checklist eden` / `checklist flycast` — fix any MISSING.
- Flycast Vulkan + gamepad transparency were set in-app after the last capture — step 1 picks them up.

## 1 · Bank the golden (the gate that makes the reset safe)
Toolkit: **`[07] Projects\odin-provisioning\`** — copy that folder to the drive/rig first
(it is not on the drive right now; this folder also borrows its `platform-tools\adb`).
1. `3-capture-golden.bat`  →  2. `save-golden.bat`  →  3. `7-pull-apks.bat`, then put the APKs on
   the games SD: `platform-tools\adb push apks /storage/9C33-6BBD/apks`
4. **GATE — all three true before any reset:** `payload\` has per-app folders ·
   `goldens\<model>\MANIFEST.txt` exists · `apks\` holds the emulators. ⛔ otherwise STOP, send `out\`.
5. Optional (2 min): `8-backup-golden.bat` → note the `.ab` size (settles Method D / m64plus).

## 2 · The method decider (~10 min, golden still intact)
From this folder: `run.bat clonetest flycast` → wait for **`[ok] clone verified on device`** →
do **NOT** open Flycast → open **ES-DE** → launch a Dreamcast game → record **BOOTS / WON'T LOAD**.
If BOOTS → repeat `clonetest dolphin` and `clonetest eden`.
(If it prints VOID, stop and bring the output back — don't judge a void run.)

## 3 · Reset + prove the loop (the real plug-and-play test)
Factory-reset the Odin. SD prompt: **"portable storage" — do NOT format** (ROMs + apks live there).
Wizard → USB debugging on → then from `odin-provisioning\`:
`2-install.bat` → `provision-all.bat` → `pair-card.bat` → `check-unit.bat` (want **PASS**) →
from ES-DE boot one game per system: **DC · GC · Switch · PS1 · SNES**. Note any folder re-pick.

## 4 · Validate the retail seal
`seal-unit.bat` (hard-stops on root or a signed-in account — FRP protection for the buyer) →
unit drops off adb (expected) → reboot it → confirm: **no Developer options, no saved Wi-Fi,
games still boot.** From now on this is the last button before boxing every unit.

## 5 · Mangmi track (independent — do while the Odin provisions)
1. USB debugging if the firmware allows → `mangmi\1-probe.bat` (best with its ~12 apps installed —
   it captures the real package names). Note whether Developer options exist at all.
2. Reset it → build its golden per **`MANGMI-SETUP.md`** Phases 0–4 + the per-unit checklist;
   note the open items (Citra graphics API, AetherSX2 renderer, full 12-app list).
3. Re-probe → if access = **rw**: `mangmi\2-capture-golden.bat` + `mangmi\save-golden.bat`.

## 6 · Bring the drive back with this filled in
```
0  cores downloaded? __        checklists clean? __
1  gate: payload __ / manifest __ / apks __        backup .ab size = ____
2  flycast ES-DE = ____   dolphin = ____   eden = ____
3  check-unit = ____   boots: DC __  GC __  Switch __  PS1 __  SNES __   re-picks: ____
4  seal clean? __   dev-options hidden after reboot? __   games still boot? __
5  mangmi access = ____   dev-options exist? __   citra gfx = ____   sx2 renderer = ____   captured? __
```
From that I lock the per-emulator recipes, pin Flycast's Vulkan/transparency values into
`recipes\flycast.txt`, finalize the Mangmi toolkit's package names — and we're in production.

## After the session (already scoped, waiting on results)
- If ES-DE = **WON'T LOAD** → script the SAF folder picks with `adb shell input tap` sequences
  (identical hardware = stable coordinates) so per-unit staff time stays near zero.
- If the m64plus `.ab` is tiny/empty → switch ES-DE's N64 system to RetroArch's mupen64plus-next
  core (kills the only /data/data emulator; test 2–3 games first).
- At volume: AYN / Mangmi **factory preload** of the golden image — Albert/Clinton supplier channel.
