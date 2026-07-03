# EDL fail-fast when no firmware resolves — design

**Date:** 2026-07-03
**Status:** Approved (Approach A; user away, proceeding on prior "do all code actionable" directive)

## Problem

`root_all` and `seal_all` (in `cas/provision.py`) flash init_boot per device. Each worker
resolves device-root firmware; when `FW.resolve` returns **no build** (`fw is None`), it
falls back to `fastboot_flasher`. For a non-EDL unit (e.g. Retroid) that is correct — it
uses the bundled default-kit init_boot. But for a **MANGMI (EDL-only)** unit — whose
bootloader fastboot **cannot write** init_boot — the fallback reboots to bootloader,
attempts a doomed fastboot flash (~30 s), and only then fails with "needs the EDL backend"
(`adb.py:447`). The message does not tell the operator the actual fix: **place the unit's
firmware build in the library** so the EDL/Firehose flasher is available.

Observed 2026-07-03 (drive 6045-F51C, `_firmware` misplaced → no air-x build resolved →
MANGMI ③ Lock un-root fell back to fastboot, unit left still-rooted / NOT sealed).

## Approach (chosen: A)

Fail **fast** — before the fastboot fallback — when the unit is EDL-only and no build
resolved, with an actionable message. Never touches non-EDL units or explicit default-kit
pins.

**Detection:** EDL-ness is a property of the firmware *build* (`is_edl` = QSaharaServer +
fh_loader present), not readable from a live device over adb. So detect the *device* as
MANGMI (hence EDL-only) via its identity: `ro.mangmi.dev.code` is present on MANGMI units
and absent elsewhere. `FW.identity()` already reads it as `dev_code`.

### Rejected

- **B — improve the late error text only.** Still wastes the reboot + doomed flash; not
  "fast."
- **C — fail-fast on *any* no-firmware unit.** Breaks the intended Retroid/default-kit
  fastboot fallback (`root_all`/`seal_all` deliberately fall back to the bundled default
  init_boot for non-EDL units).

## Design

### 1. Helper — `cas/firmware.py`

```python
def edl_only_device(identity):
    """True when a live device is EDL-only (its bootloader fastboot can't write init_boot,
    e.g. MANGMI) — detected by ro.mangmi.dev.code (identity['dev_code']). Used to fail-fast
    when no firmware build resolves, instead of a doomed fastboot flash."""
    return bool(str((identity or {}).get("dev_code") or "").strip())
```

### 2. Guard — both workers in `cas/provision.py` (`root_all`, `seal_all`)

Capture identity (currently passed inline to `resolve`) and, right after `fw =
fwres.get("firmware")`, before the fastboot fallback:

```python
idn = FW.identity(adb)
fwres = FW.resolve(serial, idn, FW.firmware_root())
fw = fwres.get("firmware")
if fw is None and fwres.get("firmware_id") != FW.DEFAULT_FW_ID and FW.edl_only_device(idn):
    msg = ("EDL-only unit (e.g. MANGMI) but no firmware build resolved — add its build "
           "under _firmware/. Not attempting a fastboot flash the bootloader can't perform.")
    log(f"[{serial}] {msg}")
    return ("fail", msg)
```

- `fw is None` — no build to flash.
- `firmware_id != DEFAULT_FW_ID` — an operator who *explicitly* pinned the bundled default
  kit is respected (their deliberate fastboot choice is not blocked).
- `edl_only_device(idn)` — only MANGMI/EDL units; Retroid & co. keep the default-kit
  fastboot fallback unchanged.
- Guard sits **inside** the existing `try` (after `resolve`), so a firmware-lookup
  *exception* still falls through to the fastboot fail-safe (unchanged).

## Testing (`tests/test_cas.py`)

Add `dev_code=""` to `FakeRunner` (backward-compatible; wired into its `getprop` map).

- `edl_only_device`: `{"dev_code":"MQ66"}`→True; `{"dev_code":""}`/`{}`/`None`→False.
- `root_all` fail-fast: FakeRunner(dev_code="MQ66", root=True) + `resolve`→no build ⇒
  result `("fail", …)` whose detail contains "firmware". (Without the guard, root=True
  would return `("ok", …)` — so a fail with that message proves the guard fired before
  `root()`.)
- `seal_all` fail-fast: same setup ⇒ `("fail", …)` detail contains "firmware".
- Default-kit-pin exemption: `resolve`→`{firmware:None, firmware_id: DEFAULT_FW_ID}` +
  dev_code set ⇒ `("ok", …)` (guard exempt → fastboot path → already-rooted early return).

Existing `test_root_all_uses_default_images_when_profile_unset` /
`test_seal_all_uses_default_init_boot_when_profile_unset` (no dev_code) still pass ⇒ the
non-EDL fallback is untouched.

## Files

- `cas/firmware.py` — `edl_only_device()`.
- `cas/provision.py` — capture `idn` + guard in `root_all` and `seal_all` workers.
- `tests/test_cas.py` — `FakeRunner.dev_code` + `TestEdlFailFast`.
