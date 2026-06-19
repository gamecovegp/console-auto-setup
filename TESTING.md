# Testing Protocol — find the path to identical, plug-and-play state

Goal: on the real Odin, determine **exactly how to get every emulator to the same playable state**
so a unit + its SD card is playable the moment it's plugged in. Run each test in order, fill in the
result, follow the arrows. Paste results back after each test and I'll tailor the next step.

**Before you start:** Odin → USB debugging ON, plugged in, "Allow" tapped. Keep this folder next to
`odin-provisioning` (for `adb`/`fastboot`). Open a command prompt here (type `cmd` in the folder's
File Explorer address bar).

---

## 📌 STATUS & DECISION — Odin2 Mini (from the runs so far)

**Device:** Odin2 Mini, Adreno 740, **Android 13**, SD `9C33-6BBD`.

**Access methods (decided):**
| Method | Result | Use |
|---|---|---|
| **A** — ADB file access | ✅ **rw** | our backbone — clones files + PLAIN configs |
| **B / B′** — root | ❌ **NO** | closed unless you unlock the bootloader (wipes device) — last resort only |
| **D** — adb backup | ❓ unconfirmed → run `run.bat backup-all` | likely dead on A13; if alive, use for `/data/data` apps (m64plus) |
| **E** — Shizuku | ✅ server runs — capped at **shell** | **CLOSED** — adds nothing over USB; tooling + notes archived to `_archive\projects\console-auto-setup\` (2026-06-11) |

**Per-emulator map (from `inspect`) + the plan for each:**
| Emulator | Map type | Plan |
|---|---|---|
| **retroarch** | PLAIN | **Method A** — download cores in-app, `setpath` overlay→0 + ROM dir, `grant`. Clones perfectly. |
| **dolphin / flycast / eden** | SAF | launch from **ES-DE** (likely makes SAF irrelevant — see hypothesis); else plain-path test; else re-pick per unit. Eden's keys/firmware/Turnip driver clone via **A** (files) — already PASS. |
| **m64plusfz** | DATA-DATA | needs `backup-all` (if alive) / root / per-unit setup. Low priority (N64). |
| **duckstation / citra / melonds / aethersx2 / ppsspp** | inspect said "NO CONFIG" but they **ARE set up** | inspect was looking at the wrong config path for these builds (e.g. PPSSPP keeps config on its memstick, Citra under `citra-emu/`). **Capture grabs each app's whole `files/` tree regardless** — the mislabel doesn't block cloning. (I can fix inspect's paths later.) |
| **esde** | frontend, PLAIN on SD | **Method A** — clones cleanly; `%ROMPATH%` portable. The launcher (see hypothesis). |

**🎯 Leading hypothesis — ES-DE makes the SAF problem disappear.** ES-DE's config is a PLAIN path on
the SD (clones via A), and it **launches games by handing the ROM to the emulator**, which generally
does **not** need that emulator's own persistent SAF folder grant. If true, the per-emulator SAF
mapping is irrelevant for plug-and-play. **Confirm with TEST 1b below.**

**Per-method next step:** **A** → finish RetroArch + clone all file-assets (keys/firmware/driver/BIOS/ES-DE). · **B/B′** → drop (no root) unless everything else fails. · **D** → `backup-all` to confirm dead, then cross off. · **E** → skip.

**▶ Ordered next actions:** (golden is fully set up — the priority is to preserve it)

> 🗓️ **2026-06-11: follow `NEXT-STEPS.md` (this folder)** — it sequences everything below
> (golden-finish → capture gate → clonetest → factory reset → provision proof → retail seal) plus
> the Mangmi Air X golden build. Use that as the session checklist; this file stays the reference.
1. **CAPTURE the golden now. Do NOT reset/wipe it until it's safely captured.** Capture grabs every
   app's whole `files/` tree + ES-DE config — use odin-provisioning's `3-capture-golden.bat` (built
   for this exact Odin), or a `capture` mode added to this toolkit.
2. **Re-run `run.bat clonetest flycast`** (the 2026-06-11 run was VOID — see TEST 1c). The fixed
   script primes the app after `pm clear` and byte-verifies the push. Wait for
   `[ok] clone verified on device`, then launch a Dreamcast game **from ES-DE** (don't open Flycast
   itself) → report **BOOTS / WON'T LOAD**. Bonus: the fresh capture now carries your Vulkan +
   gamepad-transparency values, so we can pin them into `recipes\flycast.txt` → `setpath`.
3. **Get a blank unit to clone onto** — a 2nd Odin (non-destructive), or the golden AFTER capture+reset —
   then TEST 1b *there* (ES-DE launching SAF games on a never-granted unit = the real proof).
4. **`run.bat backup-all`** — confirm Method D is dead on A13 (expected).
5. Paste results → I lock the per-emulator clone recipe.

---

## TEST 1 — Which access methods work?  ✅ DECIDED 2026-06-11 (matrix tool archived)
- Method A — ADB file access : **rw** ✅ → file cloning is the base
- Method B / B′ — root        : **NO** (bootloader locked; last resort only → Test 2)
- Method D — adb backup       : unconfirmed; optional — only matters for m64plusfz
- Method E — Shizuku          : **closed** — server runs but shell-capped; adds nothing over USB

## TEST 1b — Does ES-DE launch a SAF emulator's game ON A CLONED unit? (the decisive one)
⚠️ Must be run on a **fresh/reset unit we cloned onto via Method A — NOT your hand-set-up golden.**
On the golden, ES-DE launches everything because its SAF grants are still present from manual setup,
so it proves nothing about cloning. Procedure: capture golden → clone onto a blank unit → on *that*
unit open **ES-DE** and launch a **Dreamcast** (Flycast) + **GameCube** (Dolphin) game:
- both boot on the cloned unit? ☐ yes ☐ no

**→ Decide:** **YES** → the per-emulator SAF folder mapping is **irrelevant** for plug-and-play →
recipe = ES-DE (plain config, clones) + Method A core clone (keys/firmware/BIOS/drivers/settings) +
All-Files-Access grants. **NO** → fall back to the plain-path test (TEST 5).

## TEST 1c — Single-unit clone simulation (NO 2nd device, NO factory reset)  ★ THE test for you
You have only the set-up golden. This reproduces a "cloned onto a fresh unit" state for ONE emulator
by wiping just its app data + folder grant, pushing its captured config back, then launching via
ES-DE. It's reversible (the config is captured and re-pushed). Start with Flycast (small, SAF, no BIOS):
```
run.bat clonetest flycast
```
> ⚠️ **The 2026-06-11 first run was VOID — do not count it.** The push-back silently failed on
> Android 13 (a shell-made `Android/data/<pkg>` dir rejects file writes after `pm clear`; the
> device's `emu.cfg` ended up **0 bytes**), so Flycast started from *nothing* — "games gone +
> settings gone + had to re-pick the folder" was the broken push, **not** a verdict on Method A.
> The script now launches the app once after `pm clear` (Android recreates its dirs app-owned),
> pushes, then **byte-verifies** and prints `[ok] clone verified on device` — only judge the test
> after seeing that line.
>
> Also: **do NOT open Flycast itself after the clonetest** — its own game list is *supposed* to be
> empty (the SAF grant is wiped; that's the fresh-unit simulation). The only thing that decides the
> test is launching the game **from ES-DE**.

Then open **ES-DE** on the Odin and launch a Dreamcast game:
- **boots & plays** → ✅ Method A clone is enough; ES-DE launch doesn't need the SAF grant →
  **plug-and-play with no root.** Repeat `clonetest dolphin` / `clonetest eden` to confirm across SAF emulators.
- **won't load** → that emulator needs a per-unit folder re-pick (Method C).

**Manual fallback** (if the mode misbehaves) — for `com.flycast.emulator`:
```
adb pull /sdcard/Android/data/com.flycast.emulator/files  flycast_backup
adb shell pm clear com.flycast.emulator
adb shell mkdir -p /sdcard/Android/data/com.flycast.emulator
adb push flycast_backup\files /sdcard/Android/data/com.flycast.emulator/
adb shell appops set com.flycast.emulator MANAGE_EXTERNAL_STORAGE allow
```
…then launch from ES-DE.

> The FULL-device proof (capture golden → factory reset → full clone → verify) comes **later**, once
> this test picks the method and the golden is fully captured. TEST 1c decides the METHOD now,
> risk-free. Capture the whole golden first anyway (insurance) via `odin-provisioning\3-capture-golden.bat`.

## TEST 2 — (only if you need root and don't have it) `fastboot-check.bat`
- reached fastboot : ☐ yes ☐ no
- unlocked         : ☐ yes ☐ no

**→ Decide:** unlocked = yes → root is obtainable (temp-boot a patched image). no → no root; we lean
on **D** (backup/restore) + re-pick SAF folders per unit. Then go to **Test 3**.

## TEST 3 — How does each emulator store its game folder?  (`run.bat inspect`)
Record the **TYPE** per emulator (the tool prints it):

| Emulator | TYPE (PLAIN / SAF / DATA-DATA / NO CONFIG) |
|---|---|
| retroarch | |
| dolphin | |
| duckstation | |
| flycast | |
| citra | |
| eden | |
| melonds | |
| aethersx2 | |
| ppsspp | |
| m64plusfz | |

**→ Decide per emulator:** PLAIN → Method A · SAF → B (root) or C (re-pick) · DATA-DATA → D (backup)
or root · NO CONFIG → set the folder in-app once, then re-inspect.

## TEST 4 — Is each emulator set up correctly on the golden?  (`run.bat checklist <emu>`)
Run for the ones you care about first:
```
run.bat checklist eden
run.bat checklist retroarch
run.bat getcfg eden          (so I can pin the exact "disable overlay" key)
run.bat getcfg retroarch
```
Record each item PASS / MISSING / WRONG, and paste the `getcfg` lines that mention `overlay`,
`driver`, or a `/storage/` path.

## TEST 5 — Prove ONE emulator works end-to-end (the money test)
Pick a PLAIN one first (e.g. retroarch). Apply the fix, relaunch on the Odin, and confirm a game
**actually boots**:
```
run.bat grant   retroarch
run.bat setpath retroarch rgui_browser_directory "/storage/<your-sd>/ROMs"
run.bat setpath retroarch input_overlay_opacity "0.000000"
```
- games list populates? ☐ yes ☐ no   ·   a game boots? ☐ yes ☐ no
Then repeat for a SAF one (flycast) and a DATA-DATA one (m64plusfz) to prove the hard cases.

## TEST 6 — Prove identical clone across units (needs a 2nd Odin)
Capture the golden's state → apply to a fresh unit → plug its SD → confirm the same games boot with
no manual steps. (I'll add `capture` / `replay` / `verify-same` to the toolkit once Tests 1–5 lock
in the method — that's the engine that delivers true plug-and-play.)

---

## Results form (copy-paste back to me)
```
TEST 1  A=rw  B=no  B'=no  D=unconfirmed(optional)  E=closed   ✅ done 2026-06-11
TEST 2  fastboot=____  unlocked=____        (only if needed)
TEST 3  retroarch=____ dolphin=____ duckstation=____ flycast=____ citra=____
        eden=____ melonds=____ aethersx2=____ ppsspp=____ m64plusfz=____
TEST 4  eden: <paste checklist + getcfg overlay/driver lines>
        retroarch: <paste checklist + overlay line>
TEST 5  retroarch boots? ____   flycast boots? ____   m64plusfz boots? ____
```
