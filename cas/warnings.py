"""Actionable-warning catalog + evaluator for connected devices.

Pure logic only — NO adb/Tk/filesystem. The GUI assembles a per-device *snapshot* (identity, firmware
resolve, bootloader state, assigned-profile facts) during its refresh pass and a small global state, then
calls `evaluate()` to get a flat list of Warning dicts. The menu renders them; `gate()` drives pre-flight.

Each catalog entry declares which actions it GATES and HOW:
  * "block"   — the action skips that device (operator can't override): a true brick/cannot-run condition.
  * "confirm" — the action asks "proceed anyway?": a real risk the operator may knowingly accept.
No gate ⇒ "info": listed in the menu, never blocks.

Severity (shown in the UI) is the strongest gate any action gets: block ✗ > confirm ⚠ > info ℹ.
"""

# action keys, in the footer's run order
ACTIONS = ("root", "save", "download", "warmup", "lock")

_BLOCK_ALL = {a: "block" for a in ACTIONS}

# adb device-list states we treat as "can't talk to it" (everything that isn't "device" or "unauthorized")
_OFFLINE_STATES = {"offline", "recovery", "sideload", "no permissions", "bootloader", "host"}


# ---------------------------------------------------------------------------
# Catalog: code -> {title, detail, fix, gates}
# ---------------------------------------------------------------------------

CATALOG = {
    # --- connection ---
    "unauthorized": {
        "title": "unauthorized — accept the USB-debugging prompt",
        "detail": "adb sees the device but it hasn't authorized this PC.",
        "fix": "On the device, tap 'Allow' on the USB-debugging prompt (tick 'always allow'). "
               "Re-plug the cable if no prompt appears.",
        "gates": dict(_BLOCK_ALL),
    },
    "offline": {
        "title": "offline / not ready — CAS can't talk to it",
        "detail": "The device is listed but not in a usable 'device' state (offline, recovery, sideload…).",
        "fix": "Re-plug the cable, boot the unit normally, and click 'Refresh devices'.",
        "gates": dict(_BLOCK_ALL),
    },

    # --- root / brick-guard ---
    "no_flash_target": {
        "title": "cannot root: no flash target detected",
        "detail": "CAS could not read which partition holds the ramdisk (init_boot vs boot), so it can't "
                  "tell where to flash a patched image.",
        "fix": "Boot the unit normally and re-plug; if it persists the unit is in a state CAS can't read — "
               "this device can't be rooted until that's resolved.",
        "gates": {"root": "block", "lock": "block"},
    },
    "bootloader_locked": {
        "title": "cannot root: bootloader is LOCKED",
        "detail": "Flashing a patched init_boot needs an unlocked bootloader.",
        "fix": "Unlock the bootloader (OEM unlocking in Developer options, then fastboot flashing unlock) "
               "before rooting. Locked units can't be rooted.",
        "gates": {"root": "block", "lock": "block"},
    },
    "fw_flash_mismatch": {
        "title": "firmware/partition mismatch (brick risk)",
        "detail": "The assigned firmware targets a different partition than the device exposes.",
        "fix": "Pick the firmware whose flash target matches this unit, or confirm only if you're sure.",
        "gates": {"root": "confirm", "lock": "confirm"},
    },
    "fw_variant_mismatch": {
        "title": "firmware variant mismatch (cross-flash risk)",
        "detail": "The assigned firmware was built for a different device/variant (e.g. a sibling SKU).",
        "fix": "Assign the firmware that matches this unit's serial prefix / device; cross-flashing can "
               "bootloop. Confirm only if you know they're compatible.",
        "gates": {"root": "confirm", "lock": "confirm"},
    },
    "profile_model_mismatch": {
        "title": "profile model mismatch (bootloop risk)",
        "detail": "The assigned profile's model_match doesn't fit this device; Root/Lock would flash its "
                  "init_boot anyway.",
        "fix": "Assign a profile that matches this model, or confirm if it's the same chipset.",
        "gates": {"root": "confirm", "lock": "confirm"},
    },

    # --- advisory / info ---
    "no_firmware_match": {
        "title": "no library firmware matched",
        "detail": "No device-root firmware in the library matched this unit. Root still works from the "
                  "profile's / bundled init_boot — but nothing was auto-verified.",
        "fix": "Add/assign the correct firmware in the Root-images tab, or verify the image is right.",
        "gates": {},
    },
    "bootloader_unknown": {
        "title": "bootloader lock state unknown",
        "detail": "CAS couldn't read whether the bootloader is unlocked. Root assumes it is (as today).",
        "fix": "If Root fails to flash, confirm the bootloader is unlocked.",
        "gates": {},
    },
    "no_profile": {
        "title": "no profile assigned",
        "detail": "This device has no assigned profile. Download has nothing to push; Root would fall back "
                  "to the bundled init_boot.",
        "fix": "Assign a profile (dropdown → 'Assign profile → selected', or double-click the row).",
        "gates": {"download": "block", "root": "confirm", "warmup": "block"},
    },
    "no_golden": {
        "title": "no golden saved for the assigned profile",
        "detail": "The assigned profile has no saved golden, so there's nothing to download.",
        "fix": "Capture a golden first (① Save device → profile) or assign a profile that has one.",
        # warmup reads the same manifest Download would restore from — no golden means no manifest, the
        # same silent-empty-app-set path Download would hit, so it gates warmup at the same severity.
        "gates": {"download": "block", "warmup": "block"},
    },
    "identity_incomplete": {
        "title": "device serial unreadable",
        "detail": "CAS couldn't read this unit's serial, so a per-device assignment won't be remembered "
                  "across launches.",
        "fix": "Usually harmless; re-plug if you want sticky assignments to persist.",
        "gates": {},
    },

    # --- global ---
    "library_unreachable": {
        "title": "library folder not reachable",
        "detail": "The profile library path isn't a reachable directory (external drive unplugged?). "
                  "Download and Save can't read/write goldens.",
        "fix": "Set Settings → Library folder… to the drive, then click 'Refresh devices'.",
        "gates": {"download": "block", "save": "block", "warmup": "block"},
    },
    "firmware_library_empty": {
        "title": "firmware library is empty",
        "detail": "No device-root firmware is in the library, so auto-suggestion is disabled.",
        "fix": "Add firmware in the Root-images tab ('Add / update…') if you want auto-matching.",
        "gates": {},
    },
}


def _severity(gates):
    vals = set(gates.values())
    if "block" in vals:
        return "block"
    if "confirm" in vals:
        return "confirm"
    return "info"


def _mk(code, scope, serial, detail=None):
    spec = CATALOG[code]
    return {
        "code": code,
        "scope": scope,
        "serial": serial,
        "severity": _severity(spec["gates"]),
        "title": spec["title"],
        "detail": detail or spec["detail"],
        "fix": spec["fix"],
        "gates": dict(spec["gates"]),
    }


# ---------------------------------------------------------------------------
# Per-device evaluation
# ---------------------------------------------------------------------------

def _eval_device(d):
    serial = d.get("serial") or ""
    state = (d.get("state") or "").strip()

    if state == "unauthorized":
        return [_mk("unauthorized", "device", serial)]
    if state != "device":
        return [_mk("offline", "device", serial)]

    out = []
    identity = d.get("identity") or {}
    fw = d.get("fw") or {}
    boot = d.get("bootloader") or "unknown"

    # root / brick-guard
    if not identity.get("flash_target"):
        out.append(_mk("no_flash_target", "device", serial))
    if boot == "locked":
        out.append(_mk("bootloader_locked", "device", serial))
    elif boot != "unlocked":
        out.append(_mk("bootloader_unknown", "device", serial))
    if not identity.get("serial"):
        out.append(_mk("identity_incomplete", "device", serial))

    # firmware (library) resolve
    if fw.get("firmware_id") is None:
        out.append(_mk("no_firmware_match", "device", serial))
    elif not fw.get("ok"):
        fw_warns = fw.get("warnings") or []
        joined = "; ".join(fw_warns)
        flash = any("exposes" in w for w in fw_warns)
        variant = any(("!=" in w) or ("matches none of" in w) for w in fw_warns)
        if flash:
            out.append(_mk("fw_flash_mismatch", "device", serial, detail=joined or None))
        if variant or not flash:
            out.append(_mk("fw_variant_mismatch", "device", serial, detail=joined or None))

    # profile / golden
    prof = d.get("profile_name")
    if not prof or prof == "(no match)":
        out.append(_mk("no_profile", "device", serial))
    else:
        if d.get("profile_has_golden") is False:
            out.append(_mk("no_golden", "device", serial))
        if d.get("profile_model_match_ok") is False:
            out.append(_mk("profile_model_mismatch", "device", serial))

    return out


def evaluate(devices, global_state):
    """Return a flat list of Warning dicts for the given device snapshots + global state.
    Pure and total — a malformed field degrades to info/unknown, never raises."""
    out = []
    gs = global_state or {}
    if not gs.get("library_reachable", True):
        out.append(_mk("library_unreachable", "global", None))
    if gs.get("firmware_library_empty"):
        out.append(_mk("firmware_library_empty", "global", None))
    for d in (devices or []):
        try:
            out.extend(_eval_device(d))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Consumers
# ---------------------------------------------------------------------------

def count_actionable(warnings):
    """Warnings that need operator action (blockers + advisories) — drives the menu '(N)'."""
    return sum(1 for w in warnings if w["severity"] in ("block", "confirm"))


def gate(warnings, serial, actions):
    """Partition the warnings relevant to (serial, actions) into block vs confirm.
    serial=None selects GLOBAL warnings only; a real serial selects that device's warnings only."""
    block, confirm = [], []
    for w in warnings:
        if serial is None:
            if w["scope"] != "global":
                continue
        else:
            if w["scope"] != "device" or w["serial"] != serial:
                continue
        verdict = None
        for a in actions:
            g = w["gates"].get(a)
            if g == "block":
                verdict = "block"
                break
            if g == "confirm":
                verdict = "confirm"
        if verdict == "block":
            block.append(w)
        elif verdict == "confirm":
            confirm.append(w)
    return {"block": block, "confirm": confirm}
