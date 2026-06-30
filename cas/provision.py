"""The provision flow (PC-sourced): push the manifest's modules to the device, run restore.sh over
adb, clean up, reboot. Plus batch (all devices) and capture-to-pc (build/update a profile).

The device-side engine (restore.sh / capture.sh) is unchanged; we just push + invoke it with the
CAS_PAYLOAD / CAS_MANIFEST / CAS_OUT contract. All adb pushes + device-side rc are CHECKED so a
truncated push or a failed restore can never silently ship a broken clone.
"""
import os
import re
import time
import shutil
import tarfile
import tempfile
import pathlib
import concurrent.futures

from . import BUNDLE, DATA
from . import profiles as P

CORES_SRC = DATA / "retroarch-cores"   # the curated arm64 RetroArch core set, sourced from the PC
MEDIA_SRC = DATA / "ES-DE" / "downloaded_media"   # shared ES-DE box-art pool (box/screenshot/marquee),
#   pushed per-device but kept OUT of the per-profile golden (it's ~12 GB; bundling it would balloon every
#   profile). Override the PC source with CAS_MEDIA. The golden carries only the small ES-DE config.
COMPANION_PKG = "com.gamecove.gamecove_companion"   # the GameCove Companion app's package id. It's now a
#   normal golden app (ticked in the app list); when it's in the manifest its captured module installs
#   on-device via restore.sh, and the PC-side install below refreshes it to the current PC build.
COMPANION_SRC = DATA / "Apps" / "gamecove-companion.apk"   # the current GameCove Companion build, sourced
#   from the PC (override CAS_COMPANION_APK). Installed after restore — when the app is in the manifest —
#   so every unit ships the current build even if the captured golden module is older.
DEVICE_ADMIN = f"{COMPANION_PKG}/.GcDeviceAdminReceiver"     # the Companion's DeviceAdminReceiver
RELEASE_RECEIVER = f"{COMPANION_PKG}/.GcReleaseReceiver"
RELEASE_ACTION = "com.gamecove.companion.action.RELEASE"
_LOCK_RESTRICTIONS = ("no_factory_reset", "no_safe_boot")    # dumpsys keys for the applied restrictions
_VERIFY_ATTEMPTS = 4      # poll up to 4 times before declaring lockdown failed
_VERIFY_DELAY_S = 1.0     # seconds between verify attempts (restrictions apply asynchronously on-device)

# DEFAULT ROOT images — used for ANY profile that doesn't override them, so ⓪ Root works fleet-wide with no
# per-profile picking. One bundled kit serves every profile: the stock init_boot (patched on-device, then
# flashed) + the Magisk app. APPDIR-relative so they resolve in built kits too (firmware ships under
# provision/root/, the Magisk apk under data/Apps/ beside the app). Swap these files to change the kit. A
# profile may still override via stock_init_boot / magisk_apk in its profile.meta.
DEFAULT_STOCK_INIT_BOOT = "provision/root/firmware/odin2_20231201/init_boot.img"
DEFAULT_MAGISK_APK = "data/Apps/Magisk-v30.7.apk"


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
SCRUB = BUNDLE / "provision" / "root" / "scrub.sh"          # Lock-time ship-clean scrub (usage + saves)


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


def _split_manifest_apps(pay, pkgs, axes):
    """(payload_apps, managed_apps). PAYLOAD apps carry a captured module dir under `pay` (pushed + restored
    on-device, unchanged). MANAGED apps have the apk axis ON but NO captured module — they install PC-side
    from the server store. Companion is excluded from managed: it has its own install path."""
    pay = pathlib.Path(pay)
    payload = [p for p in pkgs if (pay / p).is_dir()]
    managed = [p for p in pkgs
               if not (pay / p).is_dir() and axes.get(p, (True, True))[0] and p != COMPANION_PKG]
    return payload, managed


def _install_apk(adb, pkg, files, log):
    """adb-install one app from the PC: `install -r -g` for a single APK, `install-multiple -r -g` for a
    split set. Best-effort — a failure is a WARNING, not an abort (matches install_companion). True on OK."""
    paths = [str(f) for f in files]
    if len(paths) == 1:
        rc, _, err = adb.raw("install", "-r", "-g", paths[0])
    else:
        rc, _, err = adb.raw("install-multiple", "-r", "-g", *paths)
    if rc == 0:
        log(f"installed {pkg} from the server store ({len(paths)} file(s)).")
        return True
    log(f"warning: install of {pkg} returned {rc}: {(err or '').strip()} (continuing).")
    return False


ESHOME = "/storage/emulated/0/ES-DE"              # the device's internal ES-DE home
DEV_ES_TAR = "/data/local/tmp/cas_es_media.tar"   # transient box-art archive landing on the device (ext4)
ES_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz")  # archives the device's toybox `tar` can unpack as-is


def _es_extract_flag(name):
    """toybox `tar` flag to unpack <name> on the device — '-xzf' for gzip (.tar.gz/.tgz), else '-xf'."""
    n = name.lower()
    return "-xzf" if (n.endswith(".gz") or n.endswith(".tgz")) else "-xf"


def _push_es_archive(adb, archive, log):
    """Push ONE box-art archive to the device, unpack it into ESHOME, then delete the device-side copy.
    True on success. Never touches the PC-side file (a pre-made archive is the operator's; an on-the-fly
    temp is owned by the caller). The archive must hold a top-level 'downloaded_media/' so it lands at
    ESHOME/downloaded_media."""
    adb.shell(f"mkdir -p {ESHOME}")
    log("pushing the box-art archive (one big file — moves at USB link speed)...")
    if not adb.push(str(archive), DEV_ES_TAR):
        log("warning: box-art archive push failed (config is fine; box art can be pushed later).")
        return False
    log("unpacking the box art on the device...")
    # adb shell's exit code isn't reliable across devices/versions, so confirm via a stdout sentinel
    # (same pattern as is_golden). Extract THEN remove the on-device archive regardless of outcome.
    flag = _es_extract_flag(pathlib.Path(archive).name)
    rc, out, _ = adb.shell(f"cd {ESHOME} && tar {flag} {DEV_ES_TAR} && echo CAS_XOK")
    adb.shell(f"rm -f {DEV_ES_TAR}")
    if rc == 0 and "CAS_XOK" in out:
        log("ES-DE box art pushed (unpacked on device).")
        return True
    log("warning: on-device unpack failed (config is fine; box art can be pushed later).")
    return False


def push_es_media(adb, log=print, media_src=None):
    """Push the SHARED ES-DE box-art pool (downloaded_media) from the PC to the device's internal ES-DE
    home — a separate ~12 GB layer kept OUT of the per-profile golden. No-op if the PC source is absent,
    or if the device already has media (a re-provision shouldn't re-push 12 GB). A failed push is a
    WARNING: box art is cosmetic, the ES-DE config (gamelists/themes) already rode the golden, so the
    unit still works without it.

    Transfer method — ONE archive, not a per-file push. downloaded_media is tens of thousands of tiny
    JPG/PNGs; an `adb push <dir>` pays a sync-protocol round-trip PER FILE, so it crawls regardless of
    link speed. We move a single archive at link speed and unpack it on the device (local FS, no ADB
    per-file overhead). tar over zip on purpose: Android's toybox `tar` is far more universally present
    than `unzip`, and tar has no zip 4 GB / ZIP64 edge cases.

    The PC source (media_src / CAS_MEDIA) may be EITHER:
      • a pre-made archive file (.tar/.tar.gz/.tgz) — pushed AS-IS, no packing (keep ES-DE media as one
        compressed file on the NAS and re-push it without re-packing). Build it with:
            tar -C <ES-DE> -czf es-de-media.tar.gz downloaded_media
      • a downloaded_media/ folder (or a parent holding it) — packed on the fly into a single, NOT
        compressed tar (box art is already-compressed JPG/PNG, so gzip would burn CPU for ~0 win),
        using Python's stdlib tarfile so NO `tar` binary is needed on the PC (Windows/macOS/Linux alike).
    """
    src = pathlib.Path(media_src) if media_src else pathlib.Path(os.environ.get("CAS_MEDIA", str(MEDIA_SRC)))
    dst = f"{ESHOME}/downloaded_media"

    # --- Case A: the source is already a single archive -> push it as-is, no packing. ---
    if src.is_file() and src.name.lower().endswith(ES_ARCHIVE_SUFFIXES):
        if adb.shell(f"ls {dst} 2>/dev/null")[1].strip():
            log(f"ES-DE box art already on device — skipping the media push ({dst}).")
            return True
        return _push_es_archive(adb, src, log)

    # --- Case B: the source is a folder -> pack it into a temp tar on the fly, then push. ---
    # Accept EITHER the box-art folder itself OR a parent that holds it (e.g. the operator picks ".../ES-DE",
    # which contains downloaded_media). We pack with arcname='downloaded_media' so it always lands at
    # .../ES-DE/downloaded_media regardless of where the source sat on the PC.
    if src.name != "downloaded_media" and (src / "downloaded_media").is_dir():
        src = src / "downloaded_media"
    if src.name != "downloaded_media":
        log(f"ES-DE box art: '{src}' is not a 'downloaded_media' folder/archive — skipping push.")
        return False
    if not (src.is_dir() and any(src.iterdir())):
        return False                                    # no shared media on this PC — nothing to push
    if adb.shell(f"ls {dst} 2>/dev/null")[1].strip():
        log(f"ES-DE box art already on device — skipping the ~12 GB media push ({dst}).")
        return True

    # Stage the archive on the SAME volume as the media (space guaranteed); fall back to the system temp
    # dir if the source tree is read-only (e.g. a NAS mount).
    tdir = str(src.parent) if os.access(src.parent, os.W_OK) else None
    try:
        fd, tmp = tempfile.mkstemp(prefix="cas_es_media_", suffix=".tar", dir=tdir)
        os.close(fd)
    except OSError as e:
        log(f"warning: cannot stage the box-art archive ({e}) — skipping (box art can be pushed later).")
        return False
    tmp = pathlib.Path(tmp)
    try:
        log("packing the shared ES-DE box art into one archive (a single file pushes far faster than "
            "tens of thousands of tiny image files)...")
        try:
            with tarfile.open(tmp, "w") as tar:               # "w" == stored, NO compression
                tar.add(str(src), arcname="downloaded_media")
        except OSError as e:
            log(f"warning: could not pack box art ({e}) — skipping (config is fine; box art optional).")
            return False
        return _push_es_archive(adb, tmp, log)
    finally:
        try:
            tmp.unlink()                                       # never leave the ~12 GB PC archive behind
        except OSError:
            pass


def install_companion(adb, log=print, apk_src=None):
    """Install the GameCove Companion app from the PC (adb install pushes the apk off the PC filesystem,
    never the SD), so every provisioned unit ships with the current build. Shared across all units — a PC
    layer kept OUT of the per-profile golden. Best-effort: a missing/failed install is a WARNING, not a
    provisioning failure (the app also self-updates OTA and can be installed later)."""
    src = pathlib.Path(apk_src) if apk_src else \
        pathlib.Path(os.environ.get("CAS_COMPANION_APK", str(COMPANION_SRC)))
    if not src.is_file():
        log(f"Companion app not on this PC ({src.name}) — skipping its install (OTA self-update still applies).")
        return False
    log(f"installing the GameCove Companion app from PC: {src.name} ...")
    rc, _, err = adb.raw("install", "-r", "-g", str(src))
    if rc == 0:
        log("Companion app installed (from PC).")
        return True
    log(f"warning: Companion app install returned {rc}: {err.strip()} (provisioning still OK).")
    return False


def _is_device_owner(adb):
    """True if the Companion app is the active Device Owner on this unit."""
    rc, out, _ = adb.shell("dpm list-owners")
    return rc == 0 and COMPANION_PKG in out


def set_device_owner(adb, log=print):
    """Make the Companion the Device Owner: non-uninstallable + can block factory reset. Idempotent — a
    unit that already has the Companion as Device Owner is a success (re-assert + verify). Returns True
    only when ownership AND the lockdown restrictions are confirmed. The CALLER decides how to treat a
    False (Download treats it as a loud warning, not an abort)."""
    if _is_device_owner(adb):
        log("Companion already Device Owner — re-asserting lockdown.")
    else:
        rc, out, err = adb.shell(f"dpm set-device-owner {DEVICE_ADMIN}")
        if rc != 0 or "Success" not in out:
            log(f"Device Owner NOT set ({(err or out).strip()}). Needs a FRESH unit (no accounts / "
                "secondary users). Unit is NOT locked down.")
            return False
        log("Companion set as Device Owner.")
    adb.shell(f"am start -n {COMPANION_PKG}/.MainActivity")   # nudge so onEnabled/launch re-assert ran
    # Poll: restrictions are applied asynchronously (via onEnabled / the launched activity), so an
    # immediate readback can race and produce a false "not confirmed" even though the unit locks down
    # moments later. Retry up to _VERIFY_ATTEMPTS times with _VERIFY_DELAY_S between each attempt;
    # break early on success. Do NOT sleep after the final attempt.
    missing = list(_LOCK_RESTRICTIONS)
    for attempt in range(_VERIFY_ATTEMPTS):
        rc, dump, _ = adb.shell("dumpsys device_policy")
        missing = [r for r in _LOCK_RESTRICTIONS if r not in dump]
        if not missing:
            break
        if attempt < _VERIFY_ATTEMPTS - 1:
            time.sleep(_VERIFY_DELAY_S)
    if missing:
        log(f"Device Owner set but restrictions missing {missing} — lockdown NOT confirmed.")
        return False
    log("lockdown confirmed (non-uninstallable + factory-reset/safe-boot blocked).")
    return True


def release(adb, log=print):
    """Operator-only un-provision: tell the Companion (via a token-guarded broadcast) to drop the lockdown
    and clear Device Owner, so the unit can be factory-reset / the app uninstalled (RMA/repair/resale).
    Returns True once Device Owner is confirmed cleared. If this ever fails, an EDL/recovery wipe remains
    the hard fallback."""
    from . import config as _cfg
    if not _is_device_owner(adb):
        log("Companion is not Device Owner on this unit — nothing to release.")
        return True
    log("sending un-provision (release) broadcast to the Companion...")
    adb.shell(f"am broadcast -a {RELEASE_ACTION} -e token '{_cfg.get_release_token()}' -n {RELEASE_RECEIVER}")
    if _is_device_owner(adb):
        log("release did NOT clear Device Owner (token mismatch or app missing?). Unit still locked — "
            "retry, or fall back to an EDL/recovery wipe.")
        return False
    log("unit released — Device Owner cleared; factory reset / uninstall now permitted.")
    return True


def provision(adb, profile, log=print, dry_push=False, es_media_src=None):
    """Provision one connected device from `profile`. Returns True on success.
    es_media_src: a PC folder to PUSH ES-DE box art onto the unit's internal storage ('internal' mode).
    None (default) = 'sd' mode: nothing is pushed and restore points ES-DE at the unit's SD card."""
    model = adb.getprop("ro.product.model")
    log(f"==> device '{model}' -> profile '{profile.name}'")
    # Root FIRST. is_golden() is fail-closed (an ambiguous/blocked `su` reads as golden), so checking it
    # before root turns the common "MagiskSU grant prompt never tapped" case into a misleading "golden
    # lock" refusal. Confirming root first means a non-rooted unit gets the correct, actionable message —
    # and once root is confirmed the golden probe's `su` genuinely answers, so the safety guard still holds.
    if not adb.is_root():
        log("no root — click '⓪ Root device' first (flashes Magisk from the PC), then retry. "
            "If Magisk is already installed, open it → Superuser → enable the 'Shell' toggle "
            "(or tap Grant on the on-device Superuser prompt).")
        return False
    if adb.is_golden():
        log("REFUSING: this device carries the golden lock (.cas_golden).")
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
    flags = profile.flags()                            # @-flags from the manifest (settings/hardening/...)
    axes = profile.axes()
    from . import config as _cfg
    # PAYLOAD apps go through push + on-device restore (unchanged); MANAGED apps (apk axis, no captured
    # module) install PC-side from the server store after restore. Validate/push only the payload apps.
    pay_pkgs, managed_pkgs = _split_manifest_apps(pay, pkgs, axes)
    if not _validate_payload(pay, pay_pkgs, log):
        return False

    # RetroArch cores come from the PC (CORES_SRC), not the SD. (The app's own cores also ride data.tar;
    # this tops the set up to the full curated library.)
    push_cores = CORES_SRC.exists() and any(CORES_SRC.glob("*.so"))

    pay_bytes = P.Profile(profile.path).golden_size() if hasattr(profile, "path") else 0
    t_push = time.monotonic()                          # time push+restore -> records bytes/sec for ETAs
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

        for i, pkg in enumerate(pay_pkgs, 1):              # only the payload (captured) app modules
            log(f"pushing module {i}/{len(pay_pkgs)}: {pkg}")
            if not push(pay / pkg, f"{DEV}/payload/"):
                return False
        for f in ("global.meta", "pkglist.txt", "urigrants.xml"):
            if (pay / f).exists() and not push(pay / f, f"{DEV}/payload/"):
                return False
        if (pay / "settings").exists() and not push(pay / "settings", f"{DEV}/payload/"):
            return False
        if (pay / "homescreen").exists() and not push(pay / "homescreen", f"{DEV}/payload/"):
            return False                                   # launcher layout + wallpaper + widget map (optional)
        if (pay / "gamelauncher").exists() and not push(pay / "gamelauncher", f"{DEV}/payload/"):
            return False                                   # game-frontend emulator picks (DataStore), optional
        for pkg in pay_pkgs:                                # internal dirs for included PAYLOAD apps only
            d = P.internal_for(pkg)
            tar = pay / f"internal_{d}.tar" if d else None
            if tar and tar.exists() and not push(tar, f"{DEV}/payload/"):
                return False
        for f in (RESTORE, LIBROOT):
            if not push(f, f"{DEV}/"):
                return False
        tf = tempfile.NamedTemporaryFile(prefix="cas_manifest_", delete=False)
        tf.close()
        dev_manifest = pathlib.Path(tf.name)
        P.save_manifest(dev_manifest, pay_pkgs, flags, header=f"# {profile.name} (deploy)",
                        axes={p: axes.get(p, (True, True)) for p in pay_pkgs})
        ok_m = push(dev_manifest, f"{DEV}/manifest")
        try:
            dev_manifest.unlink()
        except OSError:
            pass
        if not ok_m:
            return False
        if push_cores:                                     # the full curated core set, FROM THE PC
            log(f"pushing RetroArch cores from PC ({sum(1 for _ in CORES_SRC.glob('*.so'))} cores)...")
            if not push(CORES_SRC, f"{DEV}/cores"):
                return False

    cores_env = f"CAS_CORES={DEV}/cores " if (push_cores and not dry_push) else ""
    # ES-DE box art: 'internal' => push a PC folder onto the unit (below) and use the internal default;
    # 'sd' (default) => leave it on the SD and tell restore to point MediaDirectory at THIS unit's card.
    es_mode = "internal" if es_media_src else "sd"
    es_env = f"CAS_ES_MEDIA={es_mode} " if "org.es_de.frontend" in pkgs else ""
    log("running restore (installs apps, restores data/keys/BIOS/cores/grants/settings)...")
    rc = adb.su_stream(                                      # stream each [ok]/[warn] line LIVE to the log
        f"{es_env}{cores_env}CAS_PAYLOAD={DEV}/payload CAS_MANIFEST={DEV}/manifest sh {DEV}/restore.sh", log)
    if rc != 0:
        log(f"restore FAILED (rc={rc}) — NOT rebooting; the unit is NOT provisioned.")
        return False
    if not dry_push and pay_bytes:                       # record throughput + which profile/device it was for
        try:
            from . import config as _cfg
            _cfg.record_download(pay_bytes, max(0.001, time.monotonic() - t_push),
                                 profile=profile.name, serial=getattr(adb, "serial", None), model=model)
        except Exception:
            pass
    if not dry_push and "org.es_de.frontend" in pkgs and es_mode == "internal":
        push_es_media(adb, log=log, media_src=es_media_src)   # opt-in: push box art onto internal storage
    if not dry_push and COMPANION_PKG in pkgs:
        install_companion(adb, log=log)                # refresh the in-manifest Companion app to the PC build
        # Lockdown rides ② Download: make the Companion the Device Owner so it's non-uninstallable and
        # factory reset is blocked. Default ON when the Companion ships; `@lockdown off` opts a profile out.
        # Best-effort, like install_companion above: a failure is a LOUD warning, not a provision abort.
        if flags.get("lockdown", "on") != "off":
            if not set_device_owner(adb, log=log):
                log("WARNING: device-owner lockdown FAILED — unit shipped UN-LOCKED (uninstallable / "
                    "factory-resettable). Ensure the unit is FRESH and re-Download to lock it.")
    if not dry_push and managed_pkgs:
        store = _cfg.apk_store_dir()
        for pkg in managed_pkgs:
            files = P.resolve_app_apk(pkg, profile, store)
            if not files:
                log(f"WARNING: '{pkg}' is in the manifest but not in the server store ({store}) and not "
                    "captured — skipped (the config wants it).")
                continue
            _install_apk(adb, pkg, files, log)
    if not dry_push:
        adb.su(f"rm -rf {DEV}")
    adb.reboot()
    log(f"==> provisioned '{profile.name}'. Rebooting; verify on device after boot.")
    return True


MAGISK_PATCH = BUNDLE / "provision" / "root" / "magisk-patch"   # aarch64 magiskboot + Magisk's boot_patch.sh
DEV_PATCH = "/data/local/tmp/cas_magiskpatch"                   # on-device workdir for the patch


def patch_init_boot_on_device(adb, stock_init_boot, dest, log=print):
    """Patch a STOCK init_boot into a Magisk-patched one ON the device, then pull the result to `dest` on
    the PC for fastboot. Uses Magisk's own boot_patch.sh + the bundled aarch64 magiskboot — which only
    REWRITE THE IMAGE FILE (no partition touched, no root needed), so it runs on a fresh stock unit. This
    is what lets root() work from a stock image with no per-profile pre-patched file. Returns True on
    success; the unit is left unchanged on failure (only an image was produced)."""
    adb.shell(f"rm -rf {DEV_PATCH}; mkdir -p {DEV_PATCH}")
    if not adb.push(str(MAGISK_PATCH) + "/.", f"{DEV_PATCH}/"):
        log("ERROR: could not push the Magisk patch toolkit to the device.")
        return False
    if not adb.push(str(stock_init_boot), f"{DEV_PATCH}/init_boot.img"):
        log("ERROR: could not push the stock init_boot to the device.")
        return False
    log("patching the stock init_boot with Magisk ON the device (boot_patch.sh)...")
    # KEEPVERITY/KEEPFORCEENCRYPT=true: leave dm-verity + encryption alone — we only want root. The shell
    # exit code isn't reliable across devices, so confirm via a stdout sentinel (same pattern as is_golden).
    rc, out, err = adb.shell(
        f"cd {DEV_PATCH} && chmod 755 magiskboot magiskinit magisk init-ld 2>/dev/null; "
        f"KEEPVERITY=true KEEPFORCEENCRYPT=true sh boot_patch.sh init_boot.img && echo CAS_PATCH_OK")
    if "CAS_PATCH_OK" not in out:
        log(f"ERROR: on-device Magisk patch failed (rc={rc}): {((err or out) or '').strip()[:200]}")
        adb.shell(f"rm -rf {DEV_PATCH}")
        return False
    ok = adb.pull(f"{DEV_PATCH}/new-boot.img", str(dest))
    adb.shell(f"rm -rf {DEV_PATCH}")
    if not ok:
        log("ERROR: could not pull the patched init_boot off the device.")
        return False
    log("on-device patch complete — Magisk-patched init_boot pulled to the PC.")
    return True


def provision_all(make_adb, devices, root="profiles", log=print, profile=None, profile_map=None,
                  parallel=True, es_media_src=None):
    """Batch DOWNLOAD: provision every connected 'device'-state unit, in PARALLEL by default (all units
    push + restore at once). Profile resolution per device: profile_map[serial] (explicit per-device) >
    `profile` (one for all) > auto-match by model. Returns {serial: (status, detail)}; failures isolated."""
    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        try:
            adb = make_adb(serial)
            if profile_map is not None and serial in profile_map:
                prof = profile_map[serial]
                if prof is None:
                    log(f"[{serial}] no profile assigned — skip")
                    return ("no-profile", "")
            elif profile is not None:
                prof = profile
            else:
                model = adb.getprop("ro.product.model")
                prof = P.match_profile(model, root)
                if not prof:
                    log(f"[{serial}] no profile matches '{model}'")
                    return ("no-profile", model)
            msgs = []                                      # remember each line so a failure can report WHY

            def _wlog(m, s=serial):
                msgs.append(m)
                log(f"[{s}] {m}")
            ok = provision(adb, prof, log=_wlog, es_media_src=es_media_src)
            if ok:
                return ("ok", prof.name)
            # The last line provision() logged before bailing IS the reason (e.g. 'no root…',
            # 'restore FAILED…'); surface it so the report says WHY, not just which profile.
            return ("fail", msgs[-1] if msgs else prof.name)
        except Exception as e:  # isolate: one device fault must not abort the whole batch
            log(f"[{serial}] ERROR: {e}")
            return ("error", str(e))
    t0 = time.monotonic()
    results = _each_device(devices, worker, parallel)
    _log_download_run(root, results, time.monotonic() - t0, log)   # whole-run record -> the NAS library
    return results


def _append_history(root, fname, rec, log=print, summary=""):
    """Append ONE JSON-line record to <history_dir>/<fname> — the centralized run history. Destination is the
    shared NAS `log_dir` when configured + reachable (unified cross-bench logs), else the library root
    (`root`). Best-effort: a write failure WARNS, never aborts; the summary shows WHERE it landed."""
    import json
    from . import config
    dest = config.history_dir(default=root)
    path = pathlib.Path(dest) / fname
    try:
        with open(path, "a", encoding="utf-8") as f:        # one JSON line per event (small -> ~atomic append)
            f.write(json.dumps(rec) + "\n")
        if summary:
            log(f"{summary}  → {path}")                      # show the exact destination (NAS vs local)
        return True
    except OSError as e:
        log(f"warning: could not write {fname} to {dest} ({e}) — is the log dir / NAS reachable?")
        return False


def _log_download_run(root, results, elapsed, log=print):
    """Append ONE whole-Download record to <library>/download-history.jsonl. Captures the run's TOTAL length
    (bytes + seconds) and every device + its profile."""
    import datetime
    devs, total = [], 0
    for serial, (status, detail) in results.items():
        e = {"serial": serial, "status": status}
        if status == "ok":
            e["profile"] = detail
            try:
                e["bytes"] = P.Profile(pathlib.Path(root) / detail).golden_size()
            except Exception:
                e["bytes"] = 0
            total += e["bytes"]
        devs.append(e)
    if not any(d["status"] in ("ok", "fail", "error") for d in devs):
        return                                              # nothing was actually provisioned -> don't log
    rec = {
        "when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_bytes": total,
        "total_secs": round(elapsed, 1),
        "ok": sum(1 for d in devs if d["status"] == "ok"),
        "failed": sum(1 for d in devs if d["status"] in ("fail", "error")),
        "devices": devs,
    }
    _append_history(root, "download-history.jsonl", rec, log,
                    summary=(f"download run logged → download-history.jsonl: {len(devs)} device(s), "
                             f"{total // 1048576} MB total in {rec['total_secs']:.0f}s"))


def seed_default_manifest(pdir, name):
    """Seed the per-profile selection manifest from the captured golden's pkglist — every captured app,
    both axes (APK + Config) on. Used after the first capture so the Apps tab shows the device's apps
    ticked (and Download has apps to restore)."""
    pdir = pathlib.Path(pdir)
    man = pdir / "manifest"
    if man.exists() and P.manifest_pkgs(man):           # operator already has a real selection — keep it
        return
    pl = pdir / "golden_root_payload" / "pkglist.txt"
    apps = [a.strip() for a in pl.read_text().splitlines() if a.strip()] if pl.exists() else []
    P.save_manifest(man, apps,
                    {"settings": "on", "hardening": "on", "grants": "on", "homescreen": "on"},
                    header=f"# {name} default manifest")


def capture_to_pc(adb, name, stamp, root="profiles", log=print, dry_pull=False):
    """Capture the connected golden into profiles/<name>/. The existing (good) profile is touched
    ONLY after the new payload is pulled AND verified — a failed capture/pull never destroys it."""
    pdir = pathlib.Path(root) / name
    cap_man = pdir / "capture-manifest"
    dest = pdir / "golden_root_payload"
    t0 = time.monotonic()
    if not dry_pull:
        adb.shell("mkdir -p /data/local/tmp/cas_scripts")
        if not adb.push(CAPTURE, "/data/local/tmp/cas_scripts/") or \
           not adb.push(LIBROOT, "/data/local/tmp/cas_scripts/"):
            log("failed to push capture scripts — aborting (existing profile untouched).")
            return False
        if cap_man.exists() and not adb.push(cap_man, "/data/local/tmp/cas_scripts/capture-manifest"):
            log("failed to push capture-manifest — aborting (existing profile untouched).")
            return False
    log("capturing golden to device temp (per-app data, BIOS, homescreen)...")
    man_env = "CAS_MANIFEST=/data/local/tmp/cas_scripts/capture-manifest " if cap_man.exists() else ""
    rc = adb.su_stream(f"{man_env}CAS_OUT={TMPCAP} sh /data/local/tmp/cas_scripts/capture.sh", log)
    if rc != 0:
        log(f"capture.sh failed (rc={rc}) — existing profile untouched.")
        return False
    if not dry_pull:
        pdir.mkdir(parents=True, exist_ok=True)
        incoming = pdir / ".incoming"
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        # Size the payload on the device so the pull can show a REAL % bar + transfer rate. adb's own
        # '[ NN%]' only renders to a TTY; piped (how we run it) the pull is silent for minutes and the UI
        # looks frozen — so we poll the bytes landing on the PC against this total instead.
        total_kb = 0
        rc_du, out_du, _ = adb.su(f"du -sk {TMPCAP}", timeout=120)
        if rc_du == 0:
            try:
                total_kb = int(out_du.split()[0])
            except (ValueError, IndexError):
                total_kb = 0
        size_hint = f"~{total_kb // 1024} MB " if total_kb else ""
        log(f"pulling captured {size_hint}payload to the PC — a multi-GB golden can take several minutes...")
        if not adb.pull_with_progress(TMPCAP, incoming, total_kb, log):  # synthetic '[ NN%]' -> progress bar
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
        # Seed the selection from the captured apps when none is set yet (covers a fresh 'New profile'
        # whose placeholder manifest has no app lines) — so the Apps tab shows the device's apps ticked
        # and Download has apps to restore. A real, operator-edited selection is preserved.
        seed_default_manifest(pdir, name)
    if not dry_pull:                                        # log the Save to the centralized NAS history
        import datetime
        b = P.Profile(pdir).golden_size()
        _append_history(root, "save-history.jsonl", {
            "when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "profile": name,
            "serial": getattr(adb, "serial", None),
            "bytes": b,
            "secs": round(time.monotonic() - t0, 1),
        }, log, summary=f"save logged → save-history.jsonl: {name} ({b // 1048576} MB)")
    log(f"==> captured golden into profiles/{name} (prev kept for rollback)")
    return True


def fastboot_flasher(fastboot, wait=True, on_critical=None):
    """Flash backend: reboot to BOOTLOADER fastboot and write the patched image to `target`. Works on units
    whose bootloader implements `flash` (Retroid/AYN/Odin). Returns callable(adb, target, image, log)->bool;
    always reboots back to the OS, never strands the unit in fastboot. `on_critical(bool)` brackets the
    actual partition write so the GUI can warn before a Cancel that could brick."""
    def _flash(adb, target, image, log):
        log(f"rebooting to bootloader to flash {target} (fastboot)...")
        adb.raw("reboot", "bootloader")
        if wait and not fastboot.wait(on_tick=lambda s: log(f"  …waiting for fastboot ({s}s)")):
            log("ERROR: device did not enter fastboot. Aborting (it should still be bootable).")
            return False
        if on_critical:
            on_critical(True)
        try:
            ok = fastboot.flash(target, image)
        finally:
            if on_critical:
                on_critical(False)
        if not ok:
            log("ERROR: patched flash failed — this bootloader's fastboot may not support 'flash' "
                "(e.g. MANGMI → needs the EDL backend). Booting back to the OS, NOT rooted.")
            fastboot.reboot()                          # never strand the unit in fastboot
            return False
        fastboot.reboot()
        return True
    return _flash


def edl_flasher(edl, geometry, wait=True, on_critical=None):
    """Flash backend using Qualcomm EDL / Firehose — for units whose bootloader fastboot can't write
    (MANGMI). Reboots to EDL, Firehose-writes the patched image to `target` at the firmware `geometry`
    (init_boot_<slot> sector/LUN), then resets. Returns callable(adb, target, image, log)->bool.
    `on_critical(bool)` brackets the Firehose write for the GUI's Cancel brick-warning."""
    def _flash(adb, target, image, log):
        log(f"rebooting to EDL to flash {target} (Firehose)...")
        adb.raw("reboot", "edl")
        port = edl.find_port(timeout=(60 if wait else 4),
                             on_tick=lambda s: log(f"  …waiting for EDL serial /dev/ttyUSB ({s}s)"))
        if not port:
            log("ERROR: no EDL serial port appeared (qcserial driver? hold power ~12s to recover).")
            return False
        with tempfile.TemporaryDirectory() as td:
            if on_critical:
                on_critical(True)
            try:
                ok = edl.flash_partition(port, target, image, geometry, td, log=log)
            finally:
                if on_critical:
                    on_critical(False)
            # ALWAYS reboot out of EDL — even on failure — so a failed flash never strands the unit on a
            # black EDL screen (init_boot is untouched unless the Firehose write actually started).
            if not edl.reset(port, td):
                log("  …could not auto-reset out of EDL; hold power ~15s if the screen stays black.")
            return ok
    return _flash


def flasher_for_firmware(firmware, fastboot, slot, version=None, runner=None, on_critical=None):
    """Pick the flash backend for a resolved Firmware (brand-agnostic root):
      * flash_method == 'edl'  -> edl_flasher using the build's bundled QSaharaServer/fh_loader/programmer
                                  and the init_boot_<slot> geometry from its rawprogram.
      * else (or no firmware)  -> fastboot_flasher.
    Returns (flasher, None) on success, or (None, reason) when EDL is required but the build lacks the
    tools/geometry — so the caller can surface a clear error instead of silently falling back. The EDL
    backend inherits the Fastboot's cancel Event so an EDL flash is cancelable too."""
    if firmware is None or firmware.flash_method != "edl":
        return fastboot_flasher(fastboot, on_critical=on_critical), None
    from .adb import Edl, subprocess_runner
    tools = firmware.edl_tools(version)
    geom = firmware.init_boot_geometry(slot, version)
    if not tools:
        return None, "EDL firmware is missing QSaharaServer/fh_loader/prog_firehose in its payload."
    if not geom:
        return None, f"EDL firmware has no rawprogram entry for init_boot{slot}."
    q, f, p = tools
    edl = Edl(q, f, p, runner=(runner or subprocess_runner), cancel=getattr(fastboot, "cancel", None))
    return edl_flasher(edl, geom, on_critical=on_critical), None


def root(adb, fastboot, stock_init_boot, magisk_apk=None, log=print, wait=True, model_match=None,
         force=False, flasher=None):
    """Root a FRESH unit — Magisk-FIRST, everything sourced from the PC (run BEFORE provision). Inverse of
    seal():
      1) install the Magisk APP from the PC (the manager — FIRST, so it's present to own root)
      2) patch the unit's OWN STOCK init_boot into a Magisk-patched one ON the device (boot_patch.sh —
         rewrites the image file, needs no root, runs on a fresh stock unit), pulled back to the PC
      3) flash the patched init_boot to the DETECTED target (init_boot_<active slot>) via fastboot
      4) verify adb-shell root; if MagiskSU hasn't granted the shell uid yet, say exactly what to tap
    No pre-patched per-profile image is needed — only the unit's stock init_boot (the profile's
    stock_init_boot). Never strands the unit in fastboot. Refuses the golden. Bootloader must be UNLOCKED.
    force=True proceeds on a model MISMATCH (e.g. a same-chipset sibling) with a loud warning."""
    log("ROOT: installing Magisk on this unit (Magisk-first, sourced from the PC).")
    flasher = flasher or fastboot_flasher(fastboot, wait=wait)   # brand-agnostic: caller passes edl_flasher for EDL units

    rooted = adb.is_root()
    # never re-flash the GOLDEN. is_golden() needs root to read the marker, so only check when rooted
    # (an UNROOTED unit can't be the protected golden — the golden ships rooted).
    if rooted and adb.is_golden():
        log("REFUSING: this device carries the golden lock (.cas_golden) — will not re-flash it.")
        return False
    # Already rooted (and not the golden) -> nothing to do; return FAST (re-flashing a live root is slow and
    # disturbs the MagiskSU shell grant, which made batch root look "stuck" on the first device).
    if rooted:
        log("already rooted — Magisk is active; nothing to flash or install. Done.")
        return True

    # --- the unit is NOT rooted ---
    # (0) model cross-check — patching/flashing another model's init_boot bricks boot. getprop needs no root.
    if model_match:
        model = adb.getprop("ro.product.model")
        if not re.search(model_match, model):
            if not force:
                log(f"REFUSING: device model '{model}' does not match profile (model_match='{model_match}'). "
                    "Wrong-model init_boot would brick the unit. Pick the matching profile, or force.")
                return False
            log(f"⚠ WARNING: device '{model}' does NOT match the profile ('{model_match}') — proceeding by "
                "FORCE. This stock init_boot is for a DIFFERENT device; if the unit bootloops, re-flash "
                "its OWN stock init_boot to recover.")

    stock = str(stock_init_boot)
    if not pathlib.Path(stock).exists():
        log(f"ERROR: stock init_boot not found on PC: {stock} — cannot root. Put the unit's stock "
            "init_boot (from its firmware) in the profile's stock_init_boot.")
        return False

    # Resolve the flash target (partition + active slot) NOW, while adb is still up — it can't be read once
    # the unit is in fastboot. Detected, not hardcoded, so a slot-B / pre-init_boot unit isn't mis-flashed.
    target = adb.boot_flash_target()

    # (1) install the Magisk APP FIRST (from the PC). Needs no root; it's the manager that owns root after
    #     the flash. `adb install` pushes the apk off the PC filesystem (never the SD), then pm-installs it.
    if magisk_apk:
        mp = str(magisk_apk)
        if not pathlib.Path(mp).exists():
            log(f"warning: Magisk apk not found on PC: {mp} — skipping app install (will still root via flash).")
        else:
            log(f"step 1/4: installing the Magisk app from PC: {pathlib.Path(mp).name} ...")
            rc, _, err = adb.raw("install", "-r", mp)
            log("Magisk app installed (from PC)." if rc == 0 else
                f"warning: Magisk app install returned {rc}: {err.strip()} (continuing to flash).")

    # (2) patch the unit's STOCK init_boot into a Magisk-patched one ON the device, pulled to a PC temp.
    with tempfile.TemporaryDirectory() as td:
        patched = str(pathlib.Path(td) / "patched_init_boot.img")
        log("step 2/4: patching the stock init_boot with Magisk on the device...")
        if not patch_init_boot_on_device(adb, stock, patched, log=log):
            log("ERROR: on-device Magisk patch failed — NOT flashing, unit unchanged.")
            return False

        # (3) flash the patched init_boot via the device's flash backend (bootloader fastboot by default;
        #     EDL/Firehose when the caller passes an edl_flasher — units whose bootloader can't write).
        log(f"step 3/4: flashing the patched {target}...")
        if not flasher(adb, target, patched, log):
            return False
    log("flashed; rebooting to system. step 4/4: waiting for the device to finish booting (1-3 min)...")
    if wait and not adb.wait_boot(on_tick=lambda s: log(f"  …still booting ({s}s)")):
        log("ERROR: unit did not boot after the root flash — investigate before retrying.")
        return False
    log("device booted.")

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


def seal(adb, fastboot, stock_init_boot, log=print, wait=True, model_match=None, force=False, flasher=None):
    """Make a provisioned unit RETAIL-READY (run AFTER provision + verify):
      1) check the stock init_boot matches THIS device model (wrong-model flash bricks boot)
      2) uninstall the Magisk app (needs root)
      3) un-root by flashing STOCK init_boot, then CONFIRM root is actually gone
      4) hide Developer Options + disable USB debugging LAST (drops adb) — after confirmed boot + un-root
    Never strands the unit in fastboot, and never disables adb on an unverified/failed seal.
    force=True proceeds on a model MISMATCH with a loud warning instead of refusing."""
    log("SEAL: locking the unit down for retail.")
    flasher = flasher or fastboot_flasher(fastboot, wait=wait)   # brand-agnostic: caller passes edl_flasher for EDL units

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

    # Detect the flash target (partition + active slot) while adb is up — lost once in fastboot. Flashing
    # stock to the wrong/idle slot would leave the unit still rooted (the post-flash is_root check catches
    # that, but targeting the live slot is what actually un-roots it).
    target = adb.boot_flash_target()

    if adb.is_root():
        # ship-clean scrub FIRST, while still rooted — clears usage traces + saved game states before
        # un-root so the unit ships factory-fresh. Additive: scrub.sh always exits 0, never blocks the seal.
        log("scrub: clearing usage traces + saved game states before un-root…")
        adb.shell("mkdir -p /data/local/tmp/cas_scripts")
        if adb.push(SCRUB, "/data/local/tmp/cas_scripts/") and adb.push(LIBROOT, "/data/local/tmp/cas_scripts/"):
            adb.su_stream("sh /data/local/tmp/cas_scripts/scrub.sh", log)
        else:
            log("warning: could not stage scrub.sh — skipping scrub (seal proceeds).")
        rc, _, err = adb.su("pm uninstall com.topjohnwu.magisk")
        if rc != 0:
            log(f"warning: Magisk app uninstall returned {rc}: {err.strip()}")
        log("removed the Magisk app.")
    else:
        log("warning: not rooted — skipping Magisk-app removal (need root); still un-rooting via flash.")

    # (3) un-root by flashing STOCK init_boot via the device's flash backend (bootloader fastboot, or
    #     EDL/Firehose for units whose bootloader can't write — caller passes an edl_flasher).
    log(f"un-rooting: flashing STOCK {target}...")
    if not flasher(adb, target, stock, log):
        log("ERROR: stock init_boot flash failed — unit is back in the OS, still rooted, NOT sealed.")
        return False
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
             profile_map=None, force_serials=None, parallel=True, on_critical=None):
    """Batch ROOT: every connected 'device'-state unit, in PARALLEL by default (all units reboot/flash at
    once — the big win, since root is reboot-dominated). Profile per device: profile_map[serial] > `profile`
    > auto-match by model. force_serials = serials to flash even on a model mismatch (a deliberate, already-
    confirmed assignment). Devices with no profile / no stock_init_boot / the golden are skipped. Returns
    {serial: (status, detail)}, failures isolated.
    (param is profiles_root, NOT root, so it can't shadow the root() function called below.)"""
    appdir = pathlib.Path(appdir) if appdir else pathlib.Path(".")
    force_serials = force_serials or set()

    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        try:
            adb = make_adb(serial)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", "")
            if adb.is_root() and adb.is_golden():
                log(f"[{serial}] is the GOLDEN — skipped (never re-root the master)")
                return ("skip-golden", "")
            if profile_map is not None and serial in profile_map:
                prof = profile_map[serial]
                if prof is None:
                    log(f"[{serial}] no profile assigned — skip")
                    return ("no-profile", "")
            elif profile is not None:
                prof = profile
            else:
                model = adb.getprop("ro.product.model")
                prof = P.match_profile(model, profiles_root)
                if not prof:
                    log(f"[{serial}] no profile matches '{model}' — skip")
                    return ("no-profile", model)
            # Default kit images for ANY profile that doesn't override them — so Root works fleet-wide
            # without per-profile picking. A profile.meta key still wins when present.
            stock_rel = prof.meta.get("stock_init_boot") or DEFAULT_STOCK_INIT_BOOT
            magisk_rel = prof.meta.get("magisk_apk") or DEFAULT_MAGISK_APK
            stock_path = P.resolve_asset(prof, appdir, stock_rel)
            fb = make_fb(serial)
            # Brand-agnostic flash: a resolved device-root-firmware supplies the unit's OWN stock init_boot
            # and — for EDL units whose bootloader fastboot can't write (e.g. MANGMI) — the Firehose flasher.
            # Fail-safe: any firmware-lookup error → fall back to the fastboot path + profile/default stock.
            flasher = None
            try:
                from . import firmware as FW
                fwres = FW.resolve(serial, FW.identity(adb), FW.firmware_root())
                fw = fwres.get("firmware")
                if fw is not None:
                    sb = fw.stock_boot_image(fwres.get("version"))
                    if sb:
                        stock_path = str(sb)            # the unit's own init_boot from its firmware build
                    if fw.flash_method == "edl":
                        flasher, reason = flasher_for_firmware(fw, fb, adb.slot_suffix(),
                                                               version=fwres.get("version"),
                                                               on_critical=on_critical)
                        if flasher is None:
                            log(f"[{serial}] EDL firmware '{fw.id}' unusable: {reason}")
                            return ("fail", reason)
            except Exception as e:
                log(f"[{serial}] firmware lookup skipped ({e}); using fastboot + profile stock")
            if flasher is None:
                flasher = fastboot_flasher(fb, on_critical=on_critical)   # default path WITH the flash marker
            ok = root(adb, fb, stock_path,
                      magisk_apk=P.resolve_asset(prof, appdir, magisk_rel),
                      log=lambda m, s=serial: log(f"[{s}] {m}"),
                      model_match=prof.meta.get("model_match"), force=(serial in force_serials),
                      flasher=flasher)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", prof.name)
            return ("ok" if ok else "fail", prof.name)
        except Exception as e:
            log(f"[{serial}] ERROR: {e}")
            return ("error", str(e))
    return _each_device(devices, worker, parallel)


def seal_all(make_adb, make_fb, devices, profiles_root="profiles", appdir=None, log=print, profile=None,
             profile_map=None, force_serials=None, parallel=True, on_critical=None):
    """Batch SEAL: every connected 'device'-state unit, in PARALLEL by default (each un-roots + reboots at
    once, mirroring root_all). Profile per device: profile_map[serial] > `profile` > auto-match by model.
    force_serials = serials to seal even on a model mismatch (deliberate, already-confirmed). The golden and
    devices with no profile / no stock_init_boot are skipped. Per-device isolated. Returns {serial: (status,
    detail)}."""
    appdir = pathlib.Path(appdir) if appdir else pathlib.Path(".")
    force_serials = force_serials or set()

    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        try:
            adb = make_adb(serial)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", "")
            if adb.is_root() and adb.is_golden():
                log(f"[{serial}] is the GOLDEN — skipped (never seal the master)")
                return ("skip-golden", "")
            if profile_map is not None and serial in profile_map:
                prof = profile_map[serial]
                if prof is None:
                    log(f"[{serial}] no profile assigned — skip")
                    return ("no-profile", "")
            elif profile is not None:
                prof = profile
            else:
                model = adb.getprop("ro.product.model")
                prof = P.match_profile(model, profiles_root)
                if not prof:
                    log(f"[{serial}] no profile matches '{model}' — skip")
                    return ("no-profile", model)
            # Mirror root_all: fall back to the bundled default kit's STOCK init_boot so ③ Lock un-roots
            # fleet-wide with no per-profile picking (a profile.meta override still wins). It's the same image
            # whose Magisk-patched form ⓪ Root flashed, so flashing it back cleanly un-roots. seal() still
            # model-checks (won't flash a wrong-model image) and CONFIRMS un-root before disabling adb.
            stock_rel = prof.meta.get("stock_init_boot") or DEFAULT_STOCK_INIT_BOOT
            stock_path = P.resolve_asset(prof, appdir, stock_rel)
            fb = make_fb(serial)
            # Brand-agnostic un-root: a resolved device-root-firmware supplies the unit's OWN stock init_boot
            # and — for EDL units whose bootloader fastboot can't write (e.g. MANGMI) — the Firehose flasher.
            # Fail-safe: any firmware-lookup error → fall back to the fastboot path + profile/default stock.
            flasher = None
            try:
                from . import firmware as FW
                fwres = FW.resolve(serial, FW.identity(adb), FW.firmware_root())
                fw = fwres.get("firmware")
                if fw is not None:
                    sb = fw.stock_boot_image(fwres.get("version"))
                    if sb:
                        stock_path = str(sb)
                    if fw.flash_method == "edl":
                        flasher, reason = flasher_for_firmware(fw, fb, adb.slot_suffix(),
                                                               version=fwres.get("version"),
                                                               on_critical=on_critical)
                        if flasher is None:
                            log(f"[{serial}] EDL firmware '{fw.id}' unusable: {reason}")
                            return ("fail", reason)
            except Exception as e:
                log(f"[{serial}] firmware lookup skipped ({e}); using fastboot + profile stock")
            if flasher is None:
                flasher = fastboot_flasher(fb, on_critical=on_critical)
            ok = seal(adb, fb, stock_path,
                      log=lambda m, s=serial: log(f"[{s}] {m}"),
                      model_match=prof.meta.get("model_match"), force=(serial in force_serials),
                      flasher=flasher)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", prof.name)
            return ("ok" if ok else "fail", prof.name)
        except Exception as e:
            log(f"[{serial}] ERROR: {e}")
            return ("error", str(e))
    return _each_device(devices, worker, parallel)
