# Windows Operator Runbook — root-clone a unit from the golden payload

Per-unit time ≈ 6–8 min, mostly unattended. Everything heavy (ROMs, payload, cores) rides the SD.

## 0) One-time Windows PC setup
1. **platform-tools** (adb + fastboot): download
   `https://dl.google.com/android/repository/platform-tools-latest-windows.zip`, extract to
   `C:\platform-tools`. Open **PowerShell** in that folder (Shift-right-click → "Open PowerShell here").
   Use `.\adb` / `.\fastboot` (or add the folder to PATH and drop the `.\`).
2. **USB drivers (one-time, fleet-wide):** `adb` works out of the box, but the **flash** interfaces don't —
   Windows doesn't ship them. adb and fastboot are *different USB interfaces*: adb's driver is present, so
   `adb reboot bootloader` works, but with no fastboot driver `fastboot devices` shows nothing and every
   `fastboot` command sits at `< waiting for any device >`.
   - **Copy the setup onto this PC first.** From the CAS Windows kit (`cas-windows.zip`) copy
     **`setup-windows.bat`** *and* the whole **`drivers\`** folder over, and **keep them together** — the
     `.bat` runs `drivers\install-drivers.ps1` beside it. Simplest: drop both into `C:\platform-tools\` so you
     end up with `C:\platform-tools\setup-windows.bat` and `C:\platform-tools\drivers\`. (If you only unzipped
     platform-tools, you don't have these yet — grab them from the kit / `scripts\` folder.)
   - **Run it once per PC:** right-click **`setup-windows.bat` → Run as administrator** (it also self-elevates).
     Watch for `[ok] ... published to the Windows driver store`. It publishes both drivers so every unit
     auto-binds on plug — **no Zadig, no per-device setup**:
     - **fastboot / bootloader** (Retroid / AYN / Odin) → so `fastboot devices` sees the unit.
     - **Qualcomm EDL 9008** (MANGMI) → so the EDL COM port appears.
   - **Confirm it took:** `pnputil /enum-drivers | Select-String "cas-fastboot|android_winusb"` must print a
     match. Empty = the driver isn't in the store (setup didn't run/finish) → re-run step 2.
   It installs PC-side drivers only; it does not touch any device. Details + manual fallbacks (Google USB
   driver, Qualcomm QDLoader) are in `drivers\README.md`. *Do not use Zadig* — it binds per unit-serial, so
   you'd have to redo it for every device.
3. **Files on the PC:** copy `magisk_patched_init_boot.img` (from
   `provision/root/firmware/odin2_20231201/`) into `C:\platform-tools`.

## 1) The SD card
Each unit's SD is a **clone of the master** (same volume serial `9C33-6BBD`), so it already carries
`golden_root_payload\`, `provision\`, `ROMs\`, `Bios\`, `ES-DE\`, `apps\`. Clone the master with
Win32DiskImager / Rufus / `dd`. Same serial = SAF grants need no rewrite. Insert the SD into the unit.

## 2) Per unit

### A. (FRESH unit only — skip if already rooted) unlock + flash root
> A factory unit must be bootloader-unlocked once. **Unlocking WIPES the unit.**
1. First boot → skip OOBE → Settings ▸ About ▸ tap **Build number** ×7 → Developer options ▸ enable
   **OEM unlocking** and **USB debugging**.
2. `.\adb reboot bootloader`
3. `.\fastboot flashing unlock`  (confirm on-device with volume keys — this wipes/reboots)
4. After it reboots, redo OOBE + USB debugging, then `.\adb reboot bootloader`
5. `.\fastboot flash init_boot_a magisk_patched_init_boot.img`
   - if it says *"Flashing is not allowed"* → `.\fastboot reboot fastboot` (enters fastbootd) then repeat step 5
6. `.\fastboot reboot`

### B. Enable adb + grant root
7. Finish OOBE (skip Wi-Fi/account is fine), enable **USB debugging** again, then:
   `.\adb devices` → tap **Allow** on the unit's screen → it should show `device`.
8. Grant the shell root **once**: open the **Magisk** app → **Superuser** tab → toggle
   **`[SharedUID] Shell`** ON. (Or run step 9 and tap **Grant** on the pop-up.)

### C. Run the restore
9. `.\adb shell /debug_ramdisk/su -c "sh /storage/9C33-6BBD/provision/root/restore.sh"`
   - watch for `[ok] RESTORE complete` (installs 12 apps, restores data/keys/BIOS/cores/grants/settings,
     fixes Android/data ownership, grants All-files access, applies battery/OTA hardening)
10. `.\adb reboot`

### D. Verify
11. Open **ES-DE** → your games list appears from `ROMs\`; boot one game per system.
12. (Optional) confirm: `.\adb shell /debug_ramdisk/su -c "sh /storage/9C33-6BBD/provision/root/verify.sh"`

## Notes / gotchas
- **Single device:** plain `.\adb` / `.\fastboot`. Multiple plugged in: add `-s <serial>` (`.\adb devices` lists serials).
- **Root survives a factory reset** (it lives in `init_boot`), so a reset unit only needs **B→C→D** (no re-flash).
- **`su` is at `/debug_ramdisk/su`** on these units (plain `su` isn't on the adb PATH) — use the full path as shown.
- **Never** pick "format SD card" on a factory reset — the SD holds the payload/ROMs.
- If the cable drops mid-run (USB flaky), reseat it, `.\adb devices`, and re-run step 9 — `restore.sh` is idempotent.

## Windows troubleshooting — `fastboot` stuck at `< waiting for any device >`
`adb reboot bootloader` works but `.\fastboot devices` / `.\fastboot flash …` just wait forever. This is
**always** the fastboot USB driver, never the cable-detection you'd fix on Linux. Work top-down:
1. **Is the driver in the store?** `pnputil /enum-drivers | Select-String "cas-fastboot|android_winusb"`.
   - **Empty** → you haven't run step (0.2). Copy `setup-windows.bat` + `drivers\` onto the PC (keep them
     together) and **Run as administrator**. Re-check — the match must appear.
2. **Bind it while the unit is IN fastboot.** `pnputil` binds on device enumeration, and the fastboot
   interface only exists in bootloader mode. So: `.\adb reboot bootloader`, then with the unit on the
   bootloader screen **unplug + replug the USB cable**, then `.\fastboot devices` → the serial appears.
3. **Still nothing?** Open `devmgmt.msc` with the unit in fastboot; a device with a **yellow triangle**
   (often *Android* / *Fastboot* under "Other devices") is the undriven interface. Right-click →
   **Update driver → Browse → Let me pick** → choose **"Android Bootloader Interface (CAS WinUSB)"**.
   (To see its real USB id: `Get-PnpDevice -PresentOnly | ? Status -ne 'OK' | ft -Auto FriendlyName,Status,InstanceId`.)
4. **Physical layer** (fastboot is pickier than adb): use a **USB 2.0 port** (black, not blue USB 3.x),
   **direct to the PC** (no hub), with a known-good **data** cable.

> If `setup-windows.bat` itself errors with `The string is missing the terminator: "` — that was an old
> non-ASCII (em-dash) bug in the shipped scripts. Use a kit dated **2026-07-08 or later** (scripts are now
> pure ASCII, CI-guarded); an older copy on the bench must be replaced with the corrected `scripts\`.
