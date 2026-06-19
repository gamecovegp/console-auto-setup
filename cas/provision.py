"""The provision flow (PC-sourced): push the manifest's modules to the device, run restore.sh over
adb, clean up, reboot. Plus batch (all devices) and capture-to-pc (build/update a profile).

The device-side engine (restore.sh / capture.sh) is unchanged; we just push + invoke it with the
CAS_PAYLOAD / CAS_MANIFEST / CAS_OUT contract. All adb pushes + device-side rc are CHECKED so a
truncated push or a failed restore can never silently ship a broken clone.
"""
import re
import time
import shutil
import pathlib
import concurrent.futures

from . import BUNDLE, ROOT
from . import profiles as P

CORES_SRC = ROOT / "retroarch-cores"   # the curated arm64 RetroArch core set, sourced from the PC


def _each_device(devices, worker, parallel, max_workers=8):
    """Run worker(serial, state)->result for every device. parallel=True runs them CONCURRENTLY (each
    device is independent — its own adb/fastboot serial), so N units reboot/flash/push at once instead
    of one-after-another. Returns {serial: result}. `worker` must return a result tuple, never raise."""
    if parallel and len(devices) > 1:
        out = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(devices))) as ex:
            futs = {ex.submit(worker, s, st): s for s, st in devices}
            for f in concurrent.futures.as_completed(futs):
                out[futs[f]] = f.result()
        return out
    return {s: worker(s, st) for s, st in devices}

DEV = "/data/local/tmp/cas"          # transient payload landing on the device (ext4, clean reads)
TMPCAP = "/data/local/tmp/cas_cap"   # capture output on the device, pulled to the PC
# Device-side scripts are small, read-only, and pushed to the device — they ship INSIDE the bundle.
RESTORE = BUNDLE / "provision" / "root" / "restore.sh"
CAPTURE = BUNDLE / "provision" / "root" / "capture.sh"
LIBROOT = BUNDLE / "provision" / "root" / "lib-root.sh"


def _validate_payload(pay, pkgs, log):
    """A corrupt/incomplete payload must NOT reach the destructive restore. Returns True if OK."""
    gm = pay / "global.meta"
    if not gm.exists() or "golden_serial=" not in gm.read_text(errors="ignore"):
        log(f"payload invalid: missing/empty global.meta at {gm}")
        return False
    if not pkgs:
        log("manifest selects no apps — nothing to provision")
        return False
    missing = [p for p in pkgs
               if not (list((pay / p / "apk").glob("*.apk")) and (pay / p / "data.tar").exists())]
    if missing:
        log(f"payload missing apk/data for: {', '.join(missing)}")
        return False
    return True


def provision(adb, profile, log=print, dry_push=False):
    """Provision one connected device from `profile`. Returns True on success."""
    model = adb.getprop("ro.product.model")
    log(f"==> device '{model}' -> profile '{profile.name}'")
    if adb.is_golden():
        log("REFUSING: this device carries the golden lock (.cas_golden).")
        return False
    if not adb.is_root():
        log("no root — click '⓪ Root device' first (flashes Magisk from the PC), then retry. "
            "If Magisk is already installed, open it → Superuser → enable the 'Shell' toggle.")
        return False
    if not adb.has_sd():
        log("REFUSING: no SD card detected. The SD carries ROMs + the volume serial; provisioning "
            "without it produces a unit with no games and risks a bad serial rewrite. Insert it and retry.")
        return False
    if not profile.payload.exists():
        log(f"profile payload missing: {profile.payload}")
        return False

    pay = profile.payload
    pkgs = profile.pkgs()
    if not _validate_payload(pay, pkgs, log):
        return False

    # RetroArch cores come from the PC (CORES_SRC), not the SD. (The app's own cores also ride data.tar;
    # this tops the set up to the full curated library.)
    push_cores = CORES_SRC.exists() and any(CORES_SRC.glob("*.so"))

    if not dry_push:
        adb.su(f"rm -rf {DEV}")
        adb.shell(f"mkdir -p {DEV}/payload")

        def push(src, dst, tries=3):
            # Retry transient push failures: parallel Download saturates the shared USB bus, and a large
            # transfer can glitch out under contention (succeeds fine on its own). A retry recovers it.
            for i in range(1, tries + 1):
                if adb.push(src, dst):
                    return True
                if i < tries:
                    log(f"push glitch ({i}/{tries}) on {pathlib.Path(str(src)).name} — retrying "
                        "(parallel transfers can saturate USB)...")
                    time.sleep(2)
            log(f"PUSH FAILED after {tries} tries: {src} — aborting (a partial push would ship a broken clone).")
            return False

        for i, pkg in enumerate(pkgs, 1):                  # only the manifest's app modules
            log(f"pushing module {i}/{len(pkgs)}: {pkg}")
            if not push(pay / pkg, f"{DEV}/payload/"):
                return False
        for f in ("global.meta", "pkglist.txt", "urigrants.xml"):
            if (pay / f).exists() and not push(pay / f, f"{DEV}/payload/"):
                return False
        if (pay / "settings").exists() and not push(pay / "settings", f"{DEV}/payload/"):
            return False
        if (pay / "homescreen").exists() and not push(pay / "homescreen", f"{DEV}/payload/"):
            return False                                   # launcher layout + wallpaper + widget map (optional)
        for pkg in pkgs:                                   # internal dirs for included apps only
            d = P.internal_for(pkg)
            tar = pay / f"internal_{d}.tar" if d else None
            if tar and tar.exists() and not push(tar, f"{DEV}/payload/"):
                return False
        for f in (RESTORE, LIBROOT):
            if not push(f, f"{DEV}/"):
                return False
        if not push(profile.manifest_path, f"{DEV}/manifest"):
            return False
        if push_cores:                                     # the full curated core set, FROM THE PC
            log(f"pushing RetroArch cores from PC ({sum(1 for _ in CORES_SRC.glob('*.so'))} cores)...")
            if not push(CORES_SRC, f"{DEV}/cores"):
                return False

    cores_env = f"CAS_CORES={DEV}/cores " if (push_cores and not dry_push) else ""
    log("running restore (installs apps, restores data/keys/BIOS/cores/grants/settings)...")
    rc = adb.su_stream(                                      # stream each [ok]/[warn] line LIVE to the log
        f"{cores_env}CAS_PAYLOAD={DEV}/payload CAS_MANIFEST={DEV}/manifest sh {DEV}/restore.sh", log)
    if rc != 0:
        log(f"restore FAILED (rc={rc}) — NOT rebooting; the unit is NOT provisioned.")
        return False
    if not dry_push:
        adb.su(f"rm -rf {DEV}")
    adb.reboot()
    log(f"==> provisioned '{profile.name}'. Rebooting; verify on device after boot.")
    return True


def provision_all(make_adb, devices, root="profiles", log=print, profile=None, parallel=True):
    """Batch DOWNLOAD: provision every connected 'device'-state unit, in PARALLEL by default (all units
    push + restore at once). If `profile` is given, that ONE profile is applied to EVERY device; else each
    is auto-matched by model. Returns {serial: (status, detail)}. Per-device failures are isolated."""
    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        try:
            adb = make_adb(serial)
            if profile is not None:
                prof = profile
            else:
                model = adb.getprop("ro.product.model")
                prof = P.match_profile(model, root)
                if not prof:
                    log(f"[{serial}] no profile matches '{model}'")
                    return ("no-profile", model)
            ok = provision(adb, prof, log=lambda m, s=serial: log(f"[{s}] {m}"))
            return ("ok" if ok else "fail", prof.name)
        except Exception as e:  # isolate: one device fault must not abort the whole batch
            log(f"[{serial}] ERROR: {e}")
            return ("error", str(e))
    return _each_device(devices, worker, parallel)


def capture_to_pc(adb, name, stamp, root="profiles", log=print, dry_pull=False):
    """Capture the connected golden into profiles/<name>/. The existing (good) profile is touched
    ONLY after the new payload is pulled AND verified — a failed capture/pull never destroys it."""
    pdir = pathlib.Path(root) / name
    dest = pdir / "golden_root_payload"
    if not dry_pull:
        adb.shell("mkdir -p /data/local/tmp/cas_scripts")
        if not adb.push(CAPTURE, "/data/local/tmp/cas_scripts/") or \
           not adb.push(LIBROOT, "/data/local/tmp/cas_scripts/"):
            log("failed to push capture scripts — aborting (existing profile untouched).")
            return False
    log("capturing golden to device temp (per-app data, BIOS, homescreen)...")
    rc = adb.su_stream(f"CAS_OUT={TMPCAP} sh /data/local/tmp/cas_scripts/capture.sh", log)
    if rc != 0:
        log(f"capture.sh failed (rc={rc}) — existing profile untouched.")
        return False
    if not dry_pull:
        pdir.mkdir(parents=True, exist_ok=True)
        incoming = pdir / ".incoming"
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        log("pulling captured payload to the PC — a multi-GB golden can take several minutes...")
        if not adb.pull_stream(TMPCAP, incoming, log):       # streams adb's '[ NN%]' -> progress bar
            log("pull failed — existing profile untouched.")
            shutil.rmtree(incoming, ignore_errors=True)
            return False
        # verify the pulled payload is complete BEFORE we touch the good one
        if not (incoming / "global.meta").exists() or not (incoming / "pkglist.txt").exists():
            log("pulled payload incomplete (no global.meta/pkglist) — existing profile untouched.")
            shutil.rmtree(incoming, ignore_errors=True)
            return False
        adb.su(f"rm -rf {TMPCAP}")
        # now safe to rotate: good -> .prev, incoming -> good
        if dest.exists() or dest.is_symlink():
            prev = pdir / "golden_root_payload.prev"
            if prev.is_symlink():
                prev.unlink()                 # a .prev symlink rollback pointer — unlink the link, not its target
            elif prev.exists():
                shutil.rmtree(prev, ignore_errors=True)
            dest.rename(prev)
        incoming.rename(dest)
        man = pdir / "manifest"
        if not man.exists():
            pl = dest / "pkglist.txt"
            apps = pl.read_text().splitlines() if pl.exists() else []
            P.save_manifest(man, [a.strip() for a in apps if a.strip()],
                            {"settings": "on", "hardening": "on", "grants": "on", "homescreen": "on"},
                            header=f"# {name} default manifest")
    log(f"==> captured golden into profiles/{name} (prev kept for rollback)")
    return True


def root(adb, fastboot, patched_init_boot, magisk_apk=None, log=print, wait=True, model_match=None,
         force=False):
    """Root a FRESH unit, everything sourced from the PC (run BEFORE provision). Inverse of seal():
      1) check the patched init_boot matches THIS device model (wrong-model flash bricks boot)
      2) flash the Magisk-PATCHED init_boot FROM THE PC via fastboot, reboot, confirm it booted
      3) install the Magisk APP FROM THE PC (adb install pushes the apk off the PC — never the SD)
      4) verify adb-shell root; if MagiskSU hasn't granted the shell uid yet, say exactly what to tap
    Never strands the unit in fastboot. Refuses to re-flash the golden. Bootloader must be UNLOCKED.
    force=True proceeds on a model MISMATCH (e.g. a same-chipset sibling) with a loud warning instead of
    refusing — the caller is responsible for having confirmed + having the unit's own stock init_boot to
    recover with."""
    log("ROOT: installing Magisk on this unit (sourced from the PC).")

    rooted = adb.is_root()
    # never re-flash the GOLDEN. is_golden() needs root to read the marker, so only check when rooted
    # (an UNROOTED unit can't be the protected golden — the golden ships rooted).
    if rooted and adb.is_golden():
        log("REFUSING: this device carries the golden lock (.cas_golden) — will not re-flash it.")
        return False
    # Already rooted (and not the golden) -> nothing to do; return FAST. Crucially we do NOT re-flash and
    # do NOT re-install Magisk: re-installing on a live root is slow (11 MB) and disturbs the MagiskSU
    # shell grant, which made batch root look "stuck"/slow on the first device.
    if rooted:
        log("already rooted — Magisk is active; nothing to flash or install. Done.")
        return True

    # --- the unit is NOT rooted: flash the patched init_boot + install the Magisk app ---
    # (1) model cross-check — flashing another model's init_boot bricks boot. getprop needs no root.
    if model_match:
        model = adb.getprop("ro.product.model")
        if not re.search(model_match, model):
            if not force:
                log(f"REFUSING: device model '{model}' does not match profile (model_match='{model_match}'). "
                    "Wrong-model init_boot would brick the unit. Pick the matching profile, or force.")
                return False
            log(f"⚠ WARNING: device '{model}' does NOT match the profile ('{model_match}') — proceeding by "
                "FORCE. This init_boot was built for a DIFFERENT device; if the unit bootloops, re-flash "
                "its OWN stock init_boot to recover.")

    patched = str(patched_init_boot)
    if not pathlib.Path(patched).exists():
        log(f"ERROR: patched init_boot not found on PC: {patched} — cannot root.")
        return False

    # (2) flash the Magisk-patched init_boot.
    log("step 1/3: rebooting to bootloader to flash the Magisk-patched init_boot (from PC)...")
    adb.raw("reboot", "bootloader")
    if wait and not fastboot.wait(on_tick=lambda s: log(f"  …waiting for fastboot ({s}s)")):
        log("ERROR: device did not enter fastboot. Aborting (it should still be bootable).")
        return False
    log("step 2/3: flashing init_boot_a (patched)...")
    if not fastboot.flash("init_boot_a", patched):
        log("ERROR: patched init_boot flash failed (is the bootloader UNLOCKED?) — booting back to "
            "the OS, NOT rooted.")
        fastboot.reboot()                              # never strand the unit in fastboot
        return False
    fastboot.reboot()
    log("flashed; rebooting to system. step 3/3: waiting for the device to finish booting (1-3 min)...")
    if wait and not adb.wait_boot(on_tick=lambda s: log(f"  …still booting ({s}s)")):
        log("ERROR: unit did not boot after the root flash — investigate before retrying.")
        return False
    log("device booted.")

    # (3) install the Magisk APP from the PC. `adb install` pushes the apk off the PC filesystem (never
    #     the SD), then pm-installs it — so the app is present to manage root.
    if magisk_apk:
        mp = str(magisk_apk)
        if not pathlib.Path(mp).exists():
            log(f"warning: Magisk apk not found on PC: {mp} — skipped app install (root via init_boot active).")
        else:
            log(f"installing the Magisk app from PC: {pathlib.Path(mp).name} ...")
            rc, _, err = adb.raw("install", "-r", mp)
            if rc == 0:
                log("Magisk app installed (from PC).")
            else:
                log(f"warning: Magisk app install returned {rc}: {err.strip()} (root via init_boot still active).")

    # (4) verify adb-shell root.
    if not wait:
        return True
    if adb.is_root():
        log("✓ ROOTED — adb shell su works. Ready to '② Download to selected device'.")
        return True
    log("init_boot flashed + Magisk installed, but the adb shell uid isn't granted root YET. One-time per "
        "unit: on the device open Magisk → Superuser → enable the 'Shell' / '[SharedUID] Shell' toggle, "
        "then retry. (MagiskSU gates the shell uid until you allow it.)")
    return False


def seal(adb, fastboot, stock_init_boot, log=print, wait=True, model_match=None, force=False):
    """Make a provisioned unit RETAIL-READY (run AFTER provision + verify):
      1) check the stock init_boot matches THIS device model (wrong-model flash bricks boot)
      2) uninstall the Magisk app (needs root)
      3) un-root by flashing STOCK init_boot, then CONFIRM root is actually gone
      4) hide Developer Options + disable USB debugging LAST (drops adb) — after confirmed boot + un-root
    Never strands the unit in fastboot, and never disables adb on an unverified/failed seal.
    force=True proceeds on a model MISMATCH with a loud warning instead of refusing."""
    log("SEAL: locking the unit down for retail.")

    # NEVER seal/un-root the GOLDEN. is_golden() needs root to read the marker, so only check when rooted
    # (the golden ships rooted). This protects the master from "Apply to ALL + Lock".
    if adb.is_root() and adb.is_golden():
        log("REFUSING: this device carries the golden lock (.cas_golden) — will not un-root the master.")
        return False

    # (1) model cross-check — flashing another model's init_boot bricks boot.
    if model_match:
        model = adb.getprop("ro.product.model")
        if not re.search(model_match, model):
            if not force:
                log(f"REFUSING: device model '{model}' does not match profile (model_match='{model_match}'). "
                    "Wrong-model init_boot would brick the unit. Pick the matching profile, or force.")
                return False
            log(f"⚠ WARNING: device '{model}' does NOT match the profile ('{model_match}') — proceeding by "
                "FORCE. This stock init_boot is for a DIFFERENT device; have the unit's own stock image ready.")

    stock = str(stock_init_boot)
    if not pathlib.Path(stock).exists():
        log(f"ERROR: stock init_boot not found: {stock} — cannot un-root. Aborting seal.")
        return False

    if adb.is_root():
        rc, _, err = adb.su("pm uninstall com.topjohnwu.magisk")
        if rc != 0:
            log(f"warning: Magisk app uninstall returned {rc}: {err.strip()}")
        log("removed the Magisk app.")
    else:
        log("warning: not rooted — skipping Magisk-app removal (need root); still un-rooting via flash.")

    # (3) un-root via STOCK init_boot.
    log("un-rooting: rebooting to bootloader to flash STOCK init_boot...")
    adb.raw("reboot", "bootloader")
    if wait and not fastboot.wait(on_tick=lambda s: log(f"  …waiting for fastboot ({s}s)")):
        log("ERROR: device did not enter fastboot. Aborting seal (device should still be bootable).")
        return False
    if not fastboot.flash("init_boot_a", stock):
        log("ERROR: stock init_boot flash failed — booting back to the (still-rooted) OS, NOT sealing.")
        fastboot.reboot()                                  # never strand the unit in fastboot
        return False
    fastboot.reboot()
    log("flashed stock init_boot; waiting for the device to finish booting (1-3 min)...")
    if wait:
        if not adb.wait_boot(on_tick=lambda s: log(f"  …still booting ({s}s)")):
            log("ERROR: unit did not boot after un-root flash — NOT disabling USB debugging. Investigate.")
            return False
        if adb.is_root():
            log("ERROR: still ROOTED after stock flash (wrong slot / no-op flash?) — NOT sealing. "
                "adb left enabled so you can retry.")
            return False
        log("confirmed un-rooted.")
    # (4) LAST retail lockdown: HIDE Developer Options, THEN disable USB debugging (which drops adb). These
    # run as the shell uid (has WRITE_SECURE_SETTINGS) so they work WITHOUT root and even post-un-root — a
    # flaky su grant can never leave Developer Options visible on a shipped unit. One call, adb_enabled last.
    adb.shell("settings put global development_settings_enabled 0; "
              "settings put secure development_settings_enabled 0; "
              "settings put global adb_enabled 0")
    log("hid Developer options + disabled USB debugging. Device is SEALED — adb will now disconnect. Done.")
    return True


def root_all(make_adb, make_fb, devices, profiles_root="profiles", appdir=None, log=print, profile=None,
             parallel=True):
    """Batch ROOT: every connected 'device'-state unit, in PARALLEL by default (all units reboot/flash at
    once — the big win, since root is reboot-dominated). If `profile` is given it's used for EVERY device;
    else each is auto-matched by model. Devices with no profile / no patched_init_boot / the golden are
    skipped. Returns {serial: (status, detail)}. Per-device failures are isolated.
    (param is profiles_root, NOT root, so it can't shadow the root() function called below.)"""
    appdir = pathlib.Path(appdir) if appdir else pathlib.Path(".")

    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        try:
            adb = make_adb(serial)
            if adb.is_root() and adb.is_golden():
                log(f"[{serial}] is the GOLDEN — skipped (never re-root the master)")
                return ("skip-golden", "")
            if profile is not None:
                prof = profile
            else:
                model = adb.getprop("ro.product.model")
                prof = P.match_profile(model, profiles_root)
                if not prof:
                    log(f"[{serial}] no profile matches '{model}' — skip")
                    return ("no-profile", model)
            patched_rel = prof.meta.get("patched_init_boot")
            if not patched_rel:
                log(f"[{serial}] profile '{prof.name}' has no patched_init_boot — skip")
                return ("no-init_boot", prof.name)
            magisk_rel = prof.meta.get("magisk_apk")
            ok = root(adb, make_fb(serial), appdir / patched_rel,
                      magisk_apk=(appdir / magisk_rel) if magisk_rel else None,
                      log=lambda m, s=serial: log(f"[{s}] {m}"),
                      model_match=prof.meta.get("model_match"))
            return ("ok" if ok else "fail", prof.name)
        except Exception as e:
            log(f"[{serial}] ERROR: {e}")
            return ("error", str(e))
    return _each_device(devices, worker, parallel)


def seal_all(make_adb, make_fb, devices, profiles_root="profiles", appdir=None, log=print, profile=None,
             parallel=True):
    """Batch SEAL: every connected 'device'-state unit, in PARALLEL by default (each un-roots + reboots at
    once, mirroring root_all). If `profile` is given that ONE profile is used for EVERY device; else each
    is auto-matched by model. The golden and devices with no profile / no stock_init_boot are skipped.
    Per-device isolated. Returns {serial: (status, detail)}."""
    appdir = pathlib.Path(appdir) if appdir else pathlib.Path(".")

    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        try:
            adb = make_adb(serial)
            if adb.is_root() and adb.is_golden():
                log(f"[{serial}] is the GOLDEN — skipped (never seal the master)")
                return ("skip-golden", "")
            if profile is not None:
                prof = profile
            else:
                model = adb.getprop("ro.product.model")
                prof = P.match_profile(model, profiles_root)
                if not prof:
                    log(f"[{serial}] no profile matches '{model}' — skip")
                    return ("no-profile", model)
            stock_rel = prof.meta.get("stock_init_boot")
            if not stock_rel:
                log(f"[{serial}] profile '{prof.name}' has no stock_init_boot — skip")
                return ("no-init_boot", prof.name)
            ok = seal(adb, make_fb(serial), appdir / stock_rel,
                      log=lambda m, s=serial: log(f"[{s}] {m}"),
                      model_match=prof.meta.get("model_match"))
            return ("ok" if ok else "fail", prof.name)
        except Exception as e:
            log(f"[{serial}] ERROR: {e}")
            return ("error", str(e))
    return _each_device(devices, worker, parallel)
