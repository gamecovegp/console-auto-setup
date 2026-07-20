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
import hashlib
import tarfile
import datetime
import tempfile
import pathlib
import concurrent.futures

from . import BUNDLE, DATA
from . import profiles as P
from . import recovery as RC
from . import initboot_store as _ibs
from .adb import boot_tick_msg, is_cancelled

# RetroArch cores: resolved at provision time via config.cores_dir() (library_root()/retroarch-cores,
# falling back to APPDIR/data) — NOT a fixed APPDIR/data path, so the set follows the CAS library drive.
MEDIA_SRC = DATA / "ES-DE" / "downloaded_media"   # shared ES-DE box-art pool (box/screenshot/marquee),
#   pushed per-device but kept OUT of the per-profile golden (it's ~12 GB; bundling it would balloon every
#   profile). Override the PC source with CAS_MEDIA. The golden carries only the small ES-DE config.
MAGISK_PKG = "com.topjohnwu.magisk"                          # store key for the Magisk app (kit APK)
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


def _validate_payload(pay, pkgs, axes, log):
    """A corrupt/incomplete payload must NOT reach the destructive restore. Returns True if OK.

    AXIS-AWARE: an app requires apk/*.apk only when its apk axis is on, and data.tar only when its
    config axis is on. An apk-only capture (axes=apk, e.g. Steam Link) legitimately carries NO data.tar
    — restore.sh skips the data phase for it — so demanding data.tar there would wrongly abort the whole
    Download. `axes` maps pkg -> (want_apk, want_config); a package absent from the map defaults to both."""
    gm = pay / "global.meta"
    if not gm.exists() or "golden_serial=" not in gm.read_text(errors="ignore"):
        log(f"payload invalid: missing/empty global.meta at {gm}")
        return False
    if not pkgs:
        log("manifest selects no apps — nothing to provision")
        return False
    missing = []
    for p in pkgs:
        want_apk, want_cfg = axes.get(p, (True, True))
        has_apk = bool(list((pay / p / "apk").glob("*.apk")))
        has_data = (pay / p / "data.tar").exists()
        if (want_apk and not has_apk) or (want_cfg and not has_data):
            missing.append(p)
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


def install_store_app_pc(store_dir, pkg, mk_adb, serials, log=print):
    """Ad-hoc install: push the store's CURRENT build of `pkg` to each serial straight from the PC
    (`adb install -r -g`; splits via install-multiple, reused from _install_apk). A plain user install —
    no profile, golden, or root. Best-effort PER DEVICE: one failure warns and continues to the rest
    (matches _install_apk). Returns {serial: ok_bool}. Empty dict + a note when the store has no current
    build for `pkg` (e.g. the library drive is unreachable), so the caller can report 'nothing installed'."""
    files = P.store_apk_files(store_dir, pkg)
    if not files:
        log(f"ad-hoc install: no current build for {pkg} in the store (is the library drive reachable?) — nothing to install.")
        return {}
    return {s: _install_apk(mk_adb(s), pkg, files, log) for s in serials}


def _kit_apk(pkg, prof, appdir, fallback_rel):
    """Resolve a KIT apk (Magisk/Companion) PC-side path: the server store's CURRENT build if present, else
    the bundled fallback via resolve_asset (profile.meta override > appdir-relative default). Store-first so
    a kit can be version-managed centrally; bundle fallback so an offline NAS never blocks rooting."""
    try:
        from . import config as _cfg
        files = P.store_apk_files(_cfg.apk_store_dir(), pkg)
        if files:
            return files[0]
    except Exception:
        pass
    return P.resolve_asset(prof, appdir, fallback_rel)


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


def _push_dir(adb, push, src, dev_parent, log, arcname=None):
    """Move a PC DIRECTORY to `dev_parent` on the device, landing at `dev_parent/<arcname|src.name>`.

    NEVER `adb push <dir>`: on Windows adb.exe reports success but transfers 0 files for a DIRECTORY
    (single-FILE pushes are fine — see adb._local). That silently produced an empty payload and the
    "no APK in payload" restore failure on the Windows bench even though Linux pushed the same profile
    cleanly. So we mirror push_es_media: pack the dir into ONE stored tar with stdlib tarfile (no PC-side
    `tar` binary needed — Windows/macOS/Linux alike), push that single file (reliable everywhere), and
    unpack it on the device with toybox `tar` (confirmed by a CAS_XOK stdout sentinel, since adb shell's
    exit code isn't reliable across devices). Returns True on success. `push` is the caller's retrying
    push closure so the archive transfer still gets retries + cancel handling."""
    src = pathlib.Path(src)
    arc = arcname or src.name
    # Stage the temp tar on the SAME volume as the source (space guaranteed); fall back to the system
    # temp dir if the source tree is read-only. Stored (no compression): the payload is already tars/APKs.
    tdir = str(src.parent) if os.access(src.parent, os.W_OK) else None
    try:
        fd, tmp = tempfile.mkstemp(prefix="cas_xfer_", suffix=".tar", dir=tdir)
        os.close(fd)
    except OSError as e:
        log(f"cannot stage transfer archive for {arc}: {e} — aborting.")
        return False
    tmp = pathlib.Path(tmp)
    dev_tar = f"{dev_parent}/_cas_xfer.tar"
    try:
        try:
            with tarfile.open(tmp, "w") as tar:
                tar.add(str(src), arcname=arc)
        except OSError as e:
            log(f"could not pack {arc}: {e} — aborting.")
            return False
        if not push(tmp, dev_tar):
            return False                                       # push() already logged WHY + retried
        # Unpack into dev_parent, then remove the on-device archive regardless of outcome.
        rc, out, _ = adb.shell(f"cd {dev_parent} && tar -xf {dev_tar} && echo CAS_XOK")
        adb.shell(f"rm -f {dev_tar}")
        if rc == 0 and "CAS_XOK" in out:
            return True
        log(f"on-device unpack of {arc} failed (rc={rc}) — aborting "
            "(a partial payload would ship a broken clone).")
        return False
    finally:
        try:
            tmp.unlink()                                       # never leave the PC-side archive behind
        except OSError:
            pass


def _free_space_note(adb, path):
    """Best-effort 'Avail' line from `df -h <path>` — attached to a pack/pull failure log so the operator
    sees WHY (a near-full /data is the usual culprit). Empty string if df is unavailable/unreadable."""
    try:
        rc, out, _ = adb.su(f"df -h {path}", timeout=30)
        if rc == 0 and out.strip():
            return out.strip().splitlines()[-1]
    except Exception:
        return ""
    return ""


def _unpack_progress(tar, total_b, log, every=10.0):
    """Yield `tar`'s members in archive order, emitting the '[ NN%]' line the GUI bar already parses.

    WHY THIS EXISTS: the pack stream ends at '[ 100%]' and extractall() of a multi-GB golden then runs for
    MINUTES with nothing on the log — so the bar sits frozen at 100 and a healthy unpack is indistinguishable
    from a hang (observed 2026-07-17: an 11-minute exFAT unpack, contended by a concurrent firmware backfill
    on the same drive, read as dead). Progress here is what tells those two apart.

    WHY A GENERATOR: extractall() consumes members lazily, so this rides the SINGLE sequential pass it already
    makes. Calling getmembers() to count first would re-read the entire archive — a second multi-GB pass on the
    slow drive that is the very reason the phase is long. Handing extractall() an iterator (rather than
    hand-rolling the loop) also keeps its directory-attribute fixup and member filtering exactly as they were:
    this changes what the phase SAYS, never what it writes.

    Position is the member's own offset INSIDE the archive, not a file count, so a golden's few big .apk/.img
    members carry the weight they actually cost instead of counting the same as a 4 KB pref file. Emission is
    throttled to a percent change or `every` seconds so a tar of many tiny files cannot flood the log.

    GRANULARITY IS PER MEMBER — a line lands only BETWEEN members, so the bar pauses for however long the
    single largest member takes (the observed 2.2 GB golden's biggest is a ~240 MB Chrome.apk ≈ 10%, so ~1 min
    of an 11-min unpack; a hypothetical one-huge-file tar would report nothing until the end and is NOT covered
    by this fix). That is the honest limit: this turns a totally silent phase into a mostly-moving one. Going
    finer means wrapping the archive fileobj to count raw reads — worth it only if a real golden ever grows a
    member big enough to make the pause read as a hang again.
    """
    last_pct, last_emit, t0 = -1, 0.0, time.monotonic()
    for m in tar:
        yield m                                            # extractall() resumes us AFTER extracting `m`
        done_b = m.offset_data + m.size
        now = time.monotonic()
        pct = min(99, done_b * 100 // total_b) if total_b > 0 else 0
        if pct != last_pct or now - last_emit >= every:
            rate = (done_b / 1048576.0) / max(0.001, now - t0)
            log(f"[ {pct}%] unpacked {done_b // 1048576} / {max(total_b, done_b) // 1048576} MB ({rate:.1f} MB/s)")
            last_pct, last_emit = pct, now
    log(f"[ 100%] unpacked {total_b // 1048576} MB")


def _pull_dir(adb, dev_dir, pc_dir, log):
    """Pull a DEVICE directory tree to `pc_dir` on the PC as ONE tar — the mirror of _push_dir, so Save is
    OS-independent too. A plain `adb pull <dir>` can silently drop files on Windows the same way
    `adb push <dir>` does, which would let a Windows bench write an INCOMPLETE golden (and the metas that
    survive would pass the completeness check). So we pack the tree with toybox `tar` — as root, since the
    capture output is root-owned — and STREAM it straight to the PC via `adb exec-out` (raw binary stdout,
    reliable on every OS, with the usual '[ NN%]' progress), then unpack it on the PC with stdlib tarfile
    (no PC-side `tar` binary needed). Streaming also means NO device-side staging archive (the old path
    wrote a full second copy of the golden onto the same partition first). See Adb.su_pack_to_file for the
    `tar -C` / no-`&&` rule that fixed 'on-device pack failed (rc=1)'. `pc_dir` ends up holding the tree's
    CONTENTS. Returns True on success."""
    total_kb = 0                                                # for the '[ NN%]' pack bar (source ~= tar size)
    rc_du, out_du, _ = adb.su(f"du -sk {dev_dir}", timeout=120)
    if rc_du == 0:
        try:
            total_kb = int(out_du.split()[0])
        except (ValueError, IndexError):
            total_kb = 0
    pc_dir = pathlib.Path(pc_dir)
    pc_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="cas_pull_", suffix=".tar", dir=str(pc_dir.parent))
    os.close(fd)
    tmp = pathlib.Path(tmp)
    try:
        rc, err = adb.su_pack_to_file(dev_dir, tmp, total_kb, log)  # tar -cf - . | -> PC file (no staging)
        if is_cancelled(rc):
            log("pack+pull cancelled.")
            return False
        if rc != 0:
            log(f"on-device pack+pull of {dev_dir} failed (rc={rc}) — aborting.")
            if err:
                log(f"  device said: {err.splitlines()[-1]}")   # e.g. 'tar: write: No space left on device'
            free = _free_space_note(adb, dev_dir)
            if free:
                log(f"  free space ({dev_dir}): {free}")
            return False
        try:
            total_b = tmp.stat().st_size                   # the '[ NN%]' denominator: bytes of archive consumed
            with tarfile.open(str(tmp), "r") as tar:
                tar.extractall(str(pc_dir), members=_unpack_progress(tar, total_b, log))
        except (OSError, tarfile.TarError) as e:
            log(f"could not unpack the pulled payload ({e}) — aborting (a partial golden is unsafe).")
            return False
        return True
    finally:
        try:
            tmp.unlink()                                       # never leave the PC-side archive behind
        except OSError:
            pass


def install_companion(adb, log=print, apk_src=None):
    """Install the GameCove Companion app from the PC (adb install pushes the apk off the PC filesystem,
    never the SD), so every provisioned unit ships with the current build. Shared across all units — a PC
    layer kept OUT of the per-profile golden. Best-effort: a missing/failed install is a WARNING, not a
    provisioning failure (the app also self-updates OTA and can be installed later)."""
    if not apk_src:                                          # prefer the server store's CURRENT Companion build
        try:
            from . import config as _cfg
            files = P.store_apk_files(_cfg.apk_store_dir(), COMPANION_PKG)
        except Exception:
            files = []
        if files:
            log("installing the GameCove Companion app from the server store ...")
            return _install_apk(adb, COMPANION_PKG, files, log)
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
            detail = (err or out).strip()
            if "Unknown admin" in detail:
                # The installed Companion APK has no device-admin receiver — a wrong/old build, NOT an
                # accounts problem. Point at the real fix instead of the misleading "fresh unit" advice.
                log(f"Device Owner NOT set — the installed Companion has no device-admin receiver "
                    f"({DEVICE_ADMIN}). Update the server-store Companion to a build that declares "
                    "GcDeviceAdminReceiver, then re-Download. Unit is NOT locked down.")
            else:
                log(f"Device Owner NOT set ({detail}). Needs a FRESH unit (no accounts / "
                    "secondary users). Unit is NOT locked down.")
            return False
        log("Companion set as Device Owner.")
    adb.shell(f"am start -n {COMPANION_PKG}/.MainActivity")   # nudge so onEnabled/launch re-assert ran
    # Poll: restrictions are applied asynchronously (via onEnabled / the launched activity), so an
    # immediate readback can race and produce a false "not confirmed" even though the unit locks down
    # moments later. Retry up to _VERIFY_ATTEMPTS times with _VERIFY_DELAY_S between each attempt;
    # break early on success. Do NOT sleep after the final attempt.
    # Verify against BOTH dumpsys sources: older Android lists DO restrictions under the admin's
    # `userRestrictions:` in `dumpsys device_policy`, but Android 14+ keeps that field EMPTY and the
    # ACTIVE restrictions surface in `dumpsys user` ("Effective"/"Device policy global restrictions").
    # Checking only device_policy false-reports "lockdown FAILED" on a correctly-locked A14 unit.
    missing = list(_LOCK_RESTRICTIONS)
    for attempt in range(_VERIFY_ATTEMPTS):
        _, dp, _ = adb.shell("dumpsys device_policy")
        _, du, _ = adb.shell("dumpsys user")
        blob = (dp or "") + "\n" + (du or "")
        missing = [r for r in _LOCK_RESTRICTIONS if r not in blob]
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
    if not pay_pkgs and managed_pkgs:
        log(f"this profile selects only store-managed app(s) ({', '.join(managed_pkgs)}) and no captured "
            "payload — a Download restores a golden payload, so there is nothing to provision. Tick at "
            "least one captured app to Download.")
        return False
    if not _validate_payload(pay, pay_pkgs, axes, log):
        return False

    # RetroArch cores come from the PC CAS LIBRARY (cores_dir() = library_root()/retroarch-cores), NOT the
    # SD. Sourced from the library so the ~2.4GB set lives with the profiles, not beside the exe.
    cores_src = _cfg.cores_dir()
    push_cores = cores_src.exists() and any(cores_src.glob("*.so"))

    pay_bytes = P.Profile(profile.path).golden_size() if hasattr(profile, "path") else 0
    t_push = time.monotonic()                          # time push+restore -> records bytes/sec for ETAs
    if not dry_push:
        adb.su(f"rm -rf {DEV}")
        adb.shell(f"mkdir -p {DEV}/payload")

        def push(src, dst, tries=3):
            # Retry transient push failures: parallel Download saturates the shared USB bus, and a large
            # transfer can glitch out under contention (succeeds fine on its own). A retry recovers it.
            # On each failure we log adb's actual reason (device offline / no space / read error) so a
            # dead push is diagnosable — not a blind "PUSH FAILED". And if the device DROPPED (offline/
            # rebooted) we wait for it to come back before retrying, since an offline device fails
            # instantly and would otherwise burn all tries in a couple of seconds against a gone unit.
            name = pathlib.Path(str(src)).name
            why = ""
            for i in range(1, tries + 1):
                ok, why = adb.push_msg(src, dst)
                if ok:
                    return True
                if adb.cancel is not None and adb.cancel.is_set():
                    log(f"⏹ cancelled — stopping the push of {name}.")
                    return False                           # operator cancelled: abort NOW, don't retry
                why = why or "no error text from adb"
                if i < tries:
                    log(f"push glitch ({i}/{tries}) on {name}: {why} — retrying "
                        "(parallel transfers can saturate USB)...")
                    if not adb.is_online():                # device dropped/rebooting, not mere contention
                        log("  device stopped responding — waiting for it to reconnect…")
                        adb.await_online(120, on_tick=lambda s: log(f"  …waiting for device to return ({s}s)"))
                    else:
                        time.sleep(2)
            log(f"PUSH FAILED after {tries} tries: {src} — {why} — aborting "
                "(a partial push would ship a broken clone).")
            return False

        # DIRECTORIES go over as a tar (adb push <dir> lands 0 files on Windows — see _push_dir); single
        # FILES below push directly (single-file pushes are reliable everywhere).
        for i, pkg in enumerate(pay_pkgs, 1):              # only the payload (captured) app modules
            log(f"pushing module {i}/{len(pay_pkgs)}: {pkg}")
            if not _push_dir(adb, push, pay / pkg, f"{DEV}/payload", log):
                return False
        for f in ("global.meta", "pkglist.txt", "urigrants.xml"):
            if (pay / f).exists() and not push(pay / f, f"{DEV}/payload/"):
                return False
        if (pay / "settings").is_dir() and not _push_dir(adb, push, pay / "settings", f"{DEV}/payload", log):
            return False
        if (pay / "homescreen").is_dir() and not _push_dir(adb, push, pay / "homescreen", f"{DEV}/payload", log):
            return False                                   # launcher layout + wallpaper + widget map (optional)
        if (pay / "gamelauncher").is_dir() and not _push_dir(adb, push, pay / "gamelauncher", f"{DEV}/payload", log):
            return False                                   # game-frontend emulator picks (DataStore), optional
        if (pay / "wifi").is_dir() and not _push_dir(adb, push, pay / "wifi", f"{DEV}/payload", log):
            return False                                   # golden's saved WiFi (@wifi) — restore_wifi clones it,
                                                           # then Lock strips it; skipped if @wifi was off at Save
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
        try:
            P.save_manifest(dev_manifest, pay_pkgs, flags, header=f"# {profile.name} (deploy)",
                            axes={p: axes.get(p, (True, True)) for p in pay_pkgs})
            ok_m = push(dev_manifest, f"{DEV}/manifest")
        finally:
            try:
                dev_manifest.unlink()
            except OSError:
                pass
        if not ok_m:
            return False
        if push_cores:                                     # the full curated core set, FROM THE PC LIBRARY
            log(f"pushing RetroArch cores from the library ({sum(1 for _ in cores_src.glob('*.so'))} "
                f"cores from {cores_src})...")
            if not _push_dir(adb, push, cores_src, DEV, log, arcname="cores"):  # -> {DEV}/cores/*.so
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
            _cfg.record_download(pay_bytes, max(0.001, time.monotonic() - t_push),
                                 profile=profile.name, serial=getattr(adb, "serial", None), model=model)
        except Exception:
            pass
    if not dry_push and "org.es_de.frontend" in pkgs and es_mode == "internal":
        push_es_media(adb, log=log, media_src=es_media_src)   # opt-in: push box art onto internal storage
    if not dry_push and COMPANION_PKG in pkgs:
        install_companion(adb, log=log)                # refresh the in-manifest Companion app to the PC build
        # Lockdown rides ② Download: make the Companion the Device Owner so it's non-uninstallable and
        # factory reset is blocked — which ALSO stamps the unit "managed by your organization". Default
        # OFF (units ship un-managed, no org banner); `@lockdown on` opts a profile IN when that
        # non-uninstallable/reset-proof behaviour is wanted. Best-effort like install_companion above:
        # a failure is a LOUD warning, not a provision abort.
        if flags.get("lockdown", "off") == "on":
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
OVERLAY_DIR = BUNDLE / "provision" / "root" / "overlay"   # overlay.d boot-grant payload (rc + cas-grant.sh)

GRANT_PERSIST = BUNDLE / "provision" / "root" / "grant-persist.sh"   # permanent shell-grant writer (root)
DEV_GRANT = "/data/local/tmp/cas_grant.sh"                           # where it lands on the device
BOOT_GRANT_MARK = "/data/local/tmp/cas_boot_grant.done"             # cas-grant.sh's boot-grant result marker
# Where the boot-grant script must LIVE for init to exec it. NOT the ramdisk copy: the service fires on
# sys.boot_completed=1, long after init switch_root's away from the initramfs, so an overlay.d copy is
# unreachable (proven absent in the booted rootfs on Odin 3 + AYN Thor). /data/local/tmp is shell-
# writable (stageable before we have root) and survives the reboot. Kept in lockstep with the literal
# path in provision/root/overlay/init.cas-grant.rc, which must stay `$`-free -- init eats $ at parse time.
BOOT_GRANT_SCRIPT = "/data/local/tmp/cas-grant.sh"
GRANT_PROMPT_BTN = r"grant"          # MagiskSU su-request "Grant" button (matched case-insensitively)
# NOTE: MAGISK_PKG ("com.topjohnwu.magisk") is defined at the top of this module; grant_shell_root's
# tap gate reuses it to fence taps to the Magisk prompt so we never mis-tap another app.


def _inject_boot_grant(adb, dev_patch, log=print):
    """Bake the overlay.d boot-grant (init.cas-grant.rc + cas-grant.sh) into the already-Magisk-
    patched {dev_patch}/new-boot.img, repacked to {dev_patch}/cas-boot.img. This is what makes the
    first `su` prompt-free: at boot magiskinit runs cas-grant.sh as root, which pre-writes the shell
    ALLOW policy. Best-effort — returns True only when the repack sentinel confirms; on False the
    caller flashes the plain new-boot.img (root still works via the auto-tap fallback)."""
    if not OVERLAY_DIR.is_dir():
        log("  ⚠ overlay payload dir missing — skipping boot-grant inject (auto-tap fallback applies).")
        return False
    files = sorted(p for p in OVERLAY_DIR.iterdir() if p.is_file())
    if not files:
        log("  ⚠ overlay payload empty — skipping boot-grant inject.")
        return False
    for f in files:
        if not adb.push(str(f), f"{dev_patch}/{f.name}"):
            log("  ⚠ could not push overlay payload — skipping boot-grant inject.")
            return False
    # Stage the script where init can actually reach it at sys.boot_completed=1. dev_patch is a scratch
    # dir that gets cleaned up, and the ramdisk copy dies at switch_root, so neither survives to run
    # time — this /data copy is the one the service execs. Push it before the flash: /data/local/tmp is
    # shell-writable, so no root is needed to place it, and it persists across the reboot.
    grant_src = OVERLAY_DIR / "cas-grant.sh"
    if not adb.push(str(grant_src), BOOT_GRANT_SCRIPT):
        log("  ⚠ could not stage cas-grant.sh on /data — skipping boot-grant inject.")
        return False
    # Separate magiskboot pass so Magisk's own boot_patch.sh stays untouched. ./magiskboot: DEV_PATCH
    # isn't on PATH. Sentinel (not rc) confirms success — exit codes are unreliable on these units.
    # Only the .rc goes into the ramdisk: magiskinit merges overlay.d/*.rc into init's config (that part
    # provably works — init starts the service), while a baked .sh would just be dead weight.
    rc, out, err = adb.shell(
        f"cd {dev_patch} && ./magiskboot unpack new-boot.img && "
        f"./magiskboot cpio ramdisk.cpio "
        f"'mkdir 0750 overlay.d' "
        f"'add 0644 overlay.d/init.cas-grant.rc init.cas-grant.rc' && "
        f"./magiskboot repack new-boot.img cas-boot.img && echo CAS_INJECT_OK")
    if "CAS_INJECT_OK" in out:
        log("  ✓ boot-grant baked into the patched init_boot (overlay.d) — su will be pre-authorized.")
        return True
    log(f"  ⚠ boot-grant inject failed: {((err or out) or '').strip()[:160]} — flashing plain image.")
    return False


def _await_boot_grant(adb, log=print, timeout=25, step=2):
    """Give the baked overlay.d boot-grant a bounded window to authorize the adb shell before root()
    falls back to the (screen-dependent) auto-tap. The boot-grant service and root()'s check both hang
    off sys.boot_completed=1, and the service waits for magiskd before writing the shell-allow policy —
    so right after boot is_root() is briefly False even when the grant is landing. We poll the boot-
    grant's OWN marker (a plain `cat`, so no `su` is run and no Grant prompt is raised while we wait) and
    confirm with is_root(). Returns True once the shell holds root via the boot-grant; False (→ fall
    back) when the marker reports it couldn't, or it never reports in within `timeout`.

    cas-grant.sh writes the marker: 'cas-grant ok …' once it writes the policy, 'cas-grant daemon-not-
    ready' if magiskd never came up. ABSENT does NOT mean overlay.d was ignored — on both devices where
    this was chased, magiskinit honored overlay.d and init really did start the service (`ro.boottime.
    cas_grant` set, dmesg 'starting service cas_grant'); the script itself was unreachable or aborted.
    Diagnose absent-marker with `dmesg | grep cas_grant` (did init start it?) plus `magiskboot cpio
    "ls -r"` on the dd'd live partition (did the inject land?) — those two split inject vs run time."""
    def rooted():
        # short-bounded: a granted su replies instantly; an ungranted one may raise a MagiskSU prompt, so
        # cap it (8s) rather than block the grace loop on the 30s is_root() default.
        return "uid=0" in adb.su("id", timeout=8)[1]
    if rooted():                                       # persisted policy / grant already landed
        return True
    for s in range(step, timeout + 1, step):
        if adb.cancel is not None and adb.cancel.is_set():
            return False
        time.sleep(step)
        mark = adb.shell(f"cat {BOOT_GRANT_MARK} 2>/dev/null", timeout=8)[1]
        if "cas-grant ok" in mark:
            if rooted():
                log(f"  ✓ boot-grant authorized the shell after ~{s}s (zero-touch, no prompt).")
                return True
            log("  boot-grant wrote the policy but the shell still isn't root — falling back.")
            return False
        if "daemon-not-ready" in mark:
            log("  boot-grant ran but magiskd wasn't ready in time — falling back to auto-grant.")
            return False
        log(f"  …waiting for the zero-touch boot-grant to authorize the shell ({s}s)")
    log("  boot-grant never reported in — falling back to auto-grant. "
        "(Diagnose: `dmesg | grep cas_grant` for whether init started it.)")
    return rooted()


def patch_init_boot_on_device(adb, stock_init_boot, dest, log=print):
    """Patch a STOCK init_boot into a Magisk-patched one ON the device, then pull the result to `dest` on
    the PC for fastboot. Uses Magisk's own boot_patch.sh + the bundled aarch64 magiskboot — which only
    REWRITE THE IMAGE FILE (no partition touched, no root needed), so it runs on a fresh stock unit. This
    is what lets root() work from a stock image with no per-profile pre-patched file. Returns True on
    success; the unit is left unchanged on failure (only an image was produced)."""
    adb.shell(f"rm -rf {DEV_PATCH}; mkdir -p {DEV_PATCH}")
    # Push the toolkit file-by-file rather than `adb push <dir>/. <dest>/`. The "/." contents idiom
    # relies on adb's dir-merge semantics AND POSIX separators; from a Windows PC str(WindowsPath) is
    # backslash-separated and the trailing "/." is brittle — which is exactly the path the frozen
    # cas-gui.exe takes. MAGISK_PATCH is a small flat dir, so an explicit per-file push is deterministic
    # on every OS and pinpoints a missing/empty toolkit instead of a blind "could not push".
    if not MAGISK_PATCH.is_dir():
        log(f"ERROR: bundled Magisk patch toolkit missing at {MAGISK_PATCH} — the build is incomplete.")
        return False
    toolkit = sorted(p for p in MAGISK_PATCH.iterdir() if p.is_file())
    if not toolkit:
        log(f"ERROR: bundled Magisk patch toolkit is empty at {MAGISK_PATCH} — the build is incomplete.")
        return False
    for f in toolkit:
        if not adb.push(str(f), f"{DEV_PATCH}/{f.name}"):
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
    from . import config as _cfg
    pull_src = f"{DEV_PATCH}/new-boot.img"
    if _cfg.bake_boot_grant() and _inject_boot_grant(adb, DEV_PATCH, log=log):
        pull_src = f"{DEV_PATCH}/cas-boot.img"
    ok = adb.pull(pull_src, str(dest))
    adb.shell(f"rm -rf {DEV_PATCH}")
    if not ok:
        log("ERROR: could not pull the patched init_boot off the device.")
        return False
    log("on-device patch complete — Magisk-patched init_boot pulled to the PC.")
    return True


def capture_factory_init_boot(adb, store_root, log=print):
    """Capture this unit's OWN factory init_boot into the per-build store, for seal() to restore later so
    the unit's device OTA still source-verifies. Read the INACTIVE A/B slot — CAS only ever flashes the
    ACTIVE slot, so the inactive one still holds the pristine factory image. ADDITIVE & NON-FATAL: any
    problem logs a warning and returns False; root() succeeds regardless. Returns True iff a valid factory
    image is now stored for this build."""
    slot = (adb.slot_suffix() or "").strip()
    if slot not in ("_a", "_b"):
        log("  factory init_boot capture skipped: no distinct inactive A/B slot on this unit.")
        return False
    inactive = "_b" if slot == "_a" else "_a"
    fp = adb.getprop("ro.build.fingerprint")
    if _ibs.has(store_root, fp):
        return True                                     # already captured for this build
    part = adb.boot_partition()               # 'init_boot' (A13+) or 'boot' (older/upgraded units)
    dev = "/data/local/tmp/cas_factory_ib.img"
    rc, _out, err = adb.su(f"dd if=/dev/block/by-name/{part}{inactive} of={dev}")
    if rc != 0:
        log(f"  factory init_boot capture skipped: could not read {part}{inactive} ({err.strip()}).")
        return False
    with tempfile.TemporaryDirectory() as td:
        local = str(pathlib.Path(td) / "factory_init_boot.img")
        pulled = adb.pull(dev, local)
        adb.su(f"rm -f {dev}")                # root removes its own dd output — one command, allowed
        if not pulled:
            log("  factory init_boot capture skipped: could not pull the dumped image off the device.")
            return False
        try:
            data = pathlib.Path(local).read_bytes()
            if not _ibs.looks_like_boot_image(data):
                log(f"  factory init_boot capture skipped: {part}{inactive} is not a valid boot image "
                    "(empty/unpopulated inactive slot) — not storing.")
                return False
            if _ibs.contains_magisk(data):
                log(f"  factory init_boot capture skipped: {part}{inactive} carries Magisk markers "
                    "(not a factory image) — not storing.")
                return False
            meta = {
                "fingerprint": fp,
                "incremental": adb.getprop("ro.build.version.incremental"),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "source_serial": str(adb.serial),
                "captured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
                    "+00:00", "Z"),
            }
            _ibs.put(store_root, fp, local, meta)
        except Exception as e:                       # capture is ADDITIVE & NON-FATAL — never raise
            log(f"  factory init_boot capture skipped: unexpected error ({e}).")
            return False
    log(f"  ✓ captured this unit's factory init_boot for build {fp} (seal will restore it → OTA stays "
        "healthy).")
    return True


def _fail_with_recovery(operation, phase, adb, fb, status, detail, log):
    """Probe the device, build recovery guidance, log it live, and return the (status, detail, Recovery)
    3-tuple the GUI surfaces. Never raises — a probe error degrades to no guidance. `fb` may be None
    (Download/Warm-up don't flash; probe_mode tolerates it)."""
    try:
        mode = RC.probe_mode(adb, fb)
        rec = RC.advise(operation, phase, mode)
        log(rec.log_block())
    except Exception as e:                       # guidance is best-effort; never mask the real failure
        log(f"(recovery hint unavailable: {e})")
        rec = None
    return (status, detail, rec)


def provision_all(make_adb, devices, root="profiles", log=print, profile=None, profile_map=None,
                  parallel=True, es_media_src=None, wait_boot=False):
    """Batch DOWNLOAD: provision every connected 'device'-state unit, in PARALLEL by default (all units
    push + restore at once). Profile resolution per device: profile_map[serial] (explicit per-device) >
    `profile` (one for all) > auto-match by model. Returns {serial: (status, detail)}; failures isolated.
    wait_boot=True blocks each worker on the post-Download reboot (parallel across the batch) so a
    following stage (Lock) never starts on an offline/rebooting unit; a unit that never returns is FAILED."""
    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        adb = fb = None                       # bound before the try so the except handler can probe safely
        try:
            adb = make_adb(serial)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", "")
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
            if ok and wait_boot:
                # A later chain step (Lock) will touch this unit — block on the fire-and-forget reboot
                # provision() just issued so seal() never starts on an offline/rebooting device. wait_boot
                # re-attaches after the reboot (adb wait-for-device) then confirms sys.boot_completed, so the
                # chain CONTINUES across the reboot. A unit that never returns is FAILED, not carried to Lock.
                _wlog("waiting for the post-download reboot before the next step (Lock)…")
                if not adb.wait_boot(on_tick=lambda s, st: _wlog(boot_tick_msg(s, st))):
                    if adb.cancel is not None and adb.cancel.is_set():
                        return ("cancelled", prof.name)
                    return _fail_with_recovery("download", "reboot", adb, None, "fail",
                                               "did not boot back after the Download reboot (Lock skipped)", _wlog)
            if ok:
                return ("ok", prof.name)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", prof.name)            # operator cancelled -> ⏹, not a ❌ failure
            # The last line provision() logged before bailing IS the reason (e.g. 'no root…',
            # 'restore FAILED…'); surface it so the report says WHY, not just which profile.
            return _fail_with_recovery("download", "push", adb, None, "fail",
                                       msgs[-1] if msgs else prof.name, _wlog)
        except Exception as e:  # isolate: one device fault must not abort the whole batch
            log(f"[{serial}] ERROR: {e}")
            return _fail_with_recovery("download", "", adb, None, "error", str(e), log)
    t0 = time.monotonic()
    results = _each_device(devices, worker, parallel)
    _log_download_run(root, results, time.monotonic() - t0, log)   # whole-run record -> the library's history
    return results


# ③ WARM UP — launch every app the unit just received, once, so it initializes against its restored
# settings and indexes its games. Without this pass an emulator that has NEVER been opened won't launch a
# game from the frontend, and every unit needs a manual "open each emulator" pass before Lock.
WARMUP_FRONTENDS = ("org.es_de.frontend", "com.handheld.launcher")   # warmed LAST — see _warmup_order
WARMUP_FOREGROUND_TIMEOUT = 15    # seconds to wait for a launched app to become the resumed activity
# Bounds EACH `dumpsys activity activities` probe inside that wait. 14 emulators indexing at once contend
# the ActivityManager lock, making a wedged probe LIKELY, not theoretical; without this bound one stuck
# call inherits adb.shell's runner-default timeout (900s) and blows the 15s budget above by 60x with no
# output — the same class of bug that once silently froze wait_boot() (see wait-for-device hang, fixed by
# polling a bounded condition instead of an open-ended blocking primitive).
WARMUP_FOREGROUND_POLL_TIMEOUT = 5


def _homescreen_bundled_pkgs(payload):
    """Package names under <payload>/homescreen/apps/ — apps `homescreen_install_missing()` (lib-root.sh)
    installs on the target whose APK is NOT in the golden payload (so they never appear in the manifest,
    and `profile.pkgs()` alone would miss them — the exact never-opened-emulator bug this step exists to
    fix, on the one install path warm-up wouldn't otherwise cover). PC-side directory read; a missing
    homescreen/apps dir is an empty list, not an error."""
    d = pathlib.Path(payload) / "homescreen" / "apps"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


def _warmup_pkgs(profile):
    """The full app set warm-up should consider for `profile`: manifest apps (profile.pkgs()) UNIONED
    with the homescreen-bundled app package names — order preserved (manifest first), deduped."""
    seen, out = set(), []
    for pkg in list(profile.pkgs()) + _homescreen_bundled_pkgs(profile.payload):
        if pkg not in seen:
            seen.add(pkg)
            out.append(pkg)
    return out


def _warmup_order(pkgs, skip):
    """The launch order for one unit: `pkgs` (the EMULATOR-filtered app set — see warmup()) first, then
    the frontends. PURE (no adb).

    The frontends go LAST because they are what the warm-up is FOR — each must open after every emulator
    has initialized so it indexes against a warm set. They're an explicit constant, not manifest-derived:
    com.handheld.launcher is a SYSTEM app on MANGMI units, and user_pkgs() lists only `-3` packages, so it
    never appears in a golden's manifest. A frontend already in `pkgs` (ES-DE usually is) is launched
    ONCE, in the frontend slot at the end — not twice."""
    skip = set(skip or ())
    apps = [p for p in pkgs if p not in skip and p not in WARMUP_FRONTENDS]
    return apps + [f for f in WARMUP_FRONTENDS if f not in skip]


def _cancel_sleep(cancel, seconds):
    """Sleep up to `seconds`, polling `cancel` (a threading.Event or None) in <=0.5s slices so a Cancel
    lands within a fraction of a second instead of at the end of a long dwell/settle. Returns False the
    moment `cancel` is found set (caller should abort immediately) — including immediately, before
    sleeping at all, if it was already set. Returns True once the full duration has elapsed (seconds<=0
    elapses immediately)."""
    if cancel is not None and cancel.is_set():
        return False
    end = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.5, remaining))
        if cancel is not None and cancel.is_set():
            return False


def warmup(adb, profile, log=print, dwell=None, skip=None, settle=None):
    """Warm up one unit: launch each of its EMULATORS once (non-emulator apps are skipped — they need no
    first-run warming), in _warmup_order, and leave them RUNNING through a settle period so the frontends
    — launched LAST, right before Lock would otherwise reboot the unit — get real background indexing
    time; then sweep every launched app out of Android recents.

    No app is force-stopped DURING the pass: launching app B simply backgrounds app A, where it keeps
    indexing — a force-stop right after a short dwell would kill a scan that had just started, which is
    the very bug this step exists to fix. The sweep runs exactly ONCE, at the very end, after `settle` —
    it is safe there because the settle is what makes "still indexing in the background" true by the time
    Lock reboots the unit.

    ADDITIVE for individual apps, like scrub.sh: an app that won't launch or never reaches the foreground
    is a [warn] naming the package (and what WAS foreground, so the log localizes it) — a warm-up MISS
    must never block a seal, including a pass where every app LAUNCHED but NONE reached the foreground
    (the foreground probe can degrade on some ROMs — that is a loud [warn], not a failure). But warming
    NOTHING AT ALL is a hard failure, checked twice: first against the LIBRARY-DERIVED app set itself
    (profile.pkgs() union homescreen-bundled apps) — independent of device state, because an empty/
    unreadable manifest (library drive unmounted, corrupt profile) is the defect being guarded against,
    not a device symptom — and second, after the pass, against how many apps actually LAUNCHED, so a unit
    where every app is absent or refuses to launch still can't report green.

    Returns False when: the device isn't rooted; it's the golden master; the library-derived app set is
    empty; the pass LAUNCHED zero apps; or the operator cancelled.

    Root FIRST, exactly like provision(). Warm-up doesn't otherwise NEED root — monkey/am/pm all run as
    shell — but the golden guard does, and is_golden() is FAIL-CLOSED (an ambiguous/blocked `su` reads as
    "golden"). Probing it without confirming root first gives a false golden-lock refusal on every unit;
    SKIPPING the probe when root is absent fails the other way, and warms the MASTER — whose dirtied
    first-run state then rides the next ① Save into every future unit's payload. Requiring root closes
    both: the probe always answers honestly, and an unrooted unit gets provision()'s actionable message
    instead of a wrong one. This costs nothing in the real flow — warm-up runs between Download and Lock,
    where the unit IS rooted."""
    from . import config as _cfg
    from . import uiauto                 # function-local, matching grant_shell_root (provision.py:1177)
    dwell = _cfg.warmup_dwell_s() if dwell is None else dwell
    settle = _cfg.warmup_settle_s() if settle is None else settle
    skip = _cfg.warmup_skip_pkgs() if skip is None else skip
    cancelled = lambda: adb.cancel is not None and adb.cancel.is_set()

    # Root FIRST (see the docstring): is_golden() is fail-closed and needs su, so confirming root is what
    # makes the golden guard trustworthy in BOTH directions — no false refusal on a real unit, and no
    # silently-warmed master. Warm-up is the first step to call su after the Download reboot, so this is
    # also where a shell grant that didn't survive that reboot surfaces, with an actionable message.
    if not adb.is_root():
        # The likely cause is a MagiskSU shell grant that didn't survive the Download reboot — warm-up is
        # the first step to call su after it. root() already knows how to re-take that grant with no human
        # tap, so try it before giving up: otherwise ONE non-persistent grant fails EVERY unit in the batch
        # at ③ and none of them reach Lock.
        if _cfg.auto_grant_shell():
            log("no root yet (the shell grant may not have survived the Download reboot) — re-taking it…")
            grant_shell_root(adb, log=log)
        if not adb.is_root():
            log("no root — click '⓪ Root device' first (flashes Magisk from the PC), then retry. "
                "Warm up needs root only to verify this unit isn't the golden master; without that check "
                "it could open every app ON the master and poison the next ① Save.")
            return False
    # NEVER warm up the GOLDEN. Ticking Warm up + 'Apply to ALL' with the master on the bench would open
    # every app on it, dirtying its recents/first-run state — and the golden is never sealed, so it's never
    # scrubbed, and the damage rides the NEXT ① Save into every future unit's payload.
    if adb.is_golden():
        log("REFUSING: this device carries the golden lock (.cas_golden).")
        return False

    # Gate on the LIBRARY-DERIVED set FIRST, independent of device state. WARMUP_FRONTENDS is appended
    # unconditionally below (a system launcher + Download-installed ES-DE are normally ALWAYS present), so
    # checking the resolved `order` for emptiness can never fire on a real unit — an unreadable/empty
    # manifest would still resolve to just the two frontends and silently report ok. Checking the set
    # BEFORE the frontends are added is what makes an empty library the loud failure it needs to be.
    lib_pkgs = _warmup_pkgs(profile)
    if not lib_pkgs:
        log("FAILING: the library-derived app set is EMPTY — profile.pkgs() returned nothing and no "
            "homescreen-bundled apps were found under the payload. Is the library drive mounted, or is "
            "this profile's manifest missing/corrupt? Reporting ok over zero apps would ship a unit with "
            "every emulator un-indexed.")
        return False
    # Warm only the EMULATORS (the apps that do first-run initialization). Non-emulator apps in the
    # manifest — Steam Link, the Companion, always-install utilities — need no warming, so opening them
    # is wasted bench time. The empty-library guard above stays on the FULL set (it catches an unmounted
    # drive / corrupt manifest, unrelated to emulator content); a readable profile that simply has no
    # emulators is a soft warn, not a failure — the frontends are appended by _warmup_order regardless
    # (they index against the warmed emulators, which is the point of the pass).
    emu_pkgs = [p for p in lib_pkgs if p in P.EMULATOR_PKGS]
    if not emu_pkgs:
        log(" [warn] no emulators in this profile's app set — warming the frontends only.")
    order = _warmup_order(emu_pkgs, skip)

    log(f"==> warm up: {len(order)} app(s) to open, {dwell:g}s each (they keep indexing in the background)")
    warmed = 0
    # pkgs the pass LAUNCHED (adb.launch() returned True). The sweep AND the fail-on-nothing check below
    # both key off THIS, not `warmed`: an app that started but never confirmed foreground is still running,
    # so it must still be swept out of the customer's recents — and it still counts as "we warmed something".
    launched = []
    for pkg in order:
        if cancelled():
            log("cancelled — stopping the warm-up pass")
            return False
        if not adb.pkg_installed(pkg):
            log(f"   skip {pkg} (not installed on this unit)")
            continue
        if not adb.launch(pkg):
            log(f" [warn] {pkg} would not launch (no launcher activity?) — skipping it")
            continue
        launched.append(pkg)             # STARTED — sweep it later even if it never reaches the foreground
        deadline = time.monotonic() + WARMUP_FOREGROUND_TIMEOUT
        while time.monotonic() < deadline:
            if f"{pkg}/" in uiauto.foreground(adb, timeout=WARMUP_FOREGROUND_POLL_TIMEOUT):
                break
            if cancelled():
                log("cancelled — stopping the warm-up pass")
                return False
            # Never sleep PAST the deadline: a plain sleep(1) overshoots a short timeout (and made the
            # tests that patch WARMUP_FOREGROUND_TIMEOUT down still burn a real second per app).
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        else:
            fg = uiauto.foreground(adb, timeout=WARMUP_FOREGROUND_POLL_TIMEOUT)
            log(f" [warn] {pkg} never reached the foreground in {WARMUP_FOREGROUND_TIMEOUT}s "
                f"(foreground was: {fg or 'nothing'}) — moving on")
            continue
        warmed += 1
        log(f" [ok]   {pkg} is up ({warmed}/{len(order)})")
        if not _cancel_sleep(adb.cancel, dwell):
            log("cancelled — stopping the warm-up pass")
            return False

    if not launched:
        why = (f"every one of the {len(lib_pkgs)} app(s) in this profile is on the warm-up skip-list"
               if not order else
               f"every one of the {len(order)} app(s) was absent from this unit or refused to launch")
        log(f"FAILING: launched 0 app(s) — {why}. This unit would ship with every emulator un-indexed.")
        return False
    if warmed == 0:
        log(f" [warn] {len(launched)} app(s) launched but NONE reached the foreground — the foreground "
            "probe may be unreliable on this ROM; continuing (a warm-up miss never blocks a seal).")

    log(f" settling {settle:g}s so the last-launched apps (the frontends) get real background indexing "
        "time before Lock reboots the unit...")
    if not _cancel_sleep(adb.cancel, settle):
        log("cancelled during the settle — stopping before the sweep")
        return False
    log(f" sweeping {len(launched)} launched app(s) out of recents before Lock...")
    for pkg in launched:
        adb.shell(f"am force-stop {pkg}")
    adb.go_home()                       # never leave a unit sitting inside an emulator
    log(f" [ok]   warm-up done — {len(launched)} app(s) launched, {warmed} confirmed in the foreground")
    return True


def warmup_all(make_adb, devices, root="profiles", log=print, profile=None, profile_map=None,
               parallel=True, dwell=None, skip=None, settle=None):
    """Batch WARM UP: open every unit's apps once, in PARALLEL by default. Profile resolution matches
    provision_all: profile_map[serial] > `profile` > auto-match by model. Returns {serial: (status,
    detail)}; failures isolated.

    A unit FAILS when warmup() returns False for a reason OTHER than a cancel — e.g. its library-derived
    app set was empty, it LAUNCHED zero apps (unreadable manifest / library drive not mounted / nothing
    installed), or it hit the golden-master guard. A warm-up MISS on SOME apps — or even every launched
    app failing to reach the foreground — is still just a [warn] inside warmup() and reports 'ok', so a
    seal is never blocked by it.
    Distinguishing fail from cancelled mirrors provision_all: warmup() itself returns a bare False either
    way, so check adb.cancel AFTER the call to tell them apart."""
    t0 = time.monotonic()
    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        adb = fb = None                       # bound before the try so the except handler can probe safely
        try:
            adb = make_adb(serial)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", "")
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
            ok = warmup(adb, prof, log=_wlog, dwell=dwell, skip=skip, settle=settle)
            if ok:
                return ("ok", prof.name)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", prof.name)            # operator cancelled -> ⏹, not a ❌ failure
            # warmup() returned False on the golden guard, the no-root guard, an empty library app set, or
            # launching NOTHING — the last line it logged before bailing IS the reason.
            #
            # The golden is deliberately reported as a FAIL here, not as the "skip-golden" that root_all
            # and seal_all use. is_golden() is FAIL-CLOSED: one garbled `su` on a perfectly NORMAL unit
            # answers "golden". skip-golden SURVIVES the chain, so labelling that misread a skip would let
            # Lock seal an un-warmed unit and report it GREEN — the exact defect this step exists to
            # remove. A fail drops the unit before Lock. A red ❌ on the master is a cosmetic cost; a
            # silently un-warmed customer unit is not, so the misread must land on the fail-SAFE side.
            return _fail_with_recovery("warmup", "launch", adb, None, "fail",
                                       msgs[-1] if msgs else prof.name, _wlog)
        except Exception as e:                  # isolate: one device fault must not abort the whole batch
            log(f"[{serial}] ERROR: {e}")
            return _fail_with_recovery("warmup", "", adb, None, "error", str(e), log)
    results = _each_device(devices, worker, parallel)
    log_run(root, "warmup", results, log, elapsed=time.monotonic() - t0)   # + BATCH wall-clock
    return results


def _append_history(root, stem, rec, log=print, summary=""):
    """Append ONE JSON-line record to <history_dir>/<stem>.<machine>.jsonl — the per-machine run history.
    Namespaced by machine so benches syncing the library by copy-paste never clobber each other's logs.
    Destination is the configured+reachable `log_dir` override else the library root (`root`). Best-effort:
    a write failure WARNS, never aborts; the summary shows WHERE it landed."""
    import json
    from . import config
    dest = config.history_dir(default=root)
    path = pathlib.Path(dest) / config.history_filename(stem)
    try:
        with open(path, "a", encoding="utf-8") as f:        # one JSON line per event (small -> ~atomic append)
            f.write(json.dumps(rec) + "\n")
        if summary:
            log(f"{summary}  → {path}")                      # show the exact destination (shared vs local)
        return True
    except OSError as e:
        log(f"warning: could not write {path.name} to {dest} ({e}) — is the log dir / library drive reachable?")
        return False


def log_run(root, action, results, log=print, elapsed=None):
    """Append ONE per-run record to <action>-history.<machine>.jsonl (action ∈ root/lock/warmup): which
    devices passed, and — the point of this — the ERROR REASON for each that failed. A successful device
    carries only its 'ok' status (no noise); a failed one carries the last line it logged before bailing.
    Best-effort via _append_history (a write failure only warns). `results` is {serial:(status, detail)}.
    Download + Save keep their own byte-carrying history; this covers the actions that had none.

    `elapsed` (seconds) is the action's BATCH WALL-CLOCK — these actions fan out across devices in
    PARALLEL, so four units rooting together for ten minutes is 600, not 2400. That is the number that
    predicts a bench day. Omitted entirely when None (never written as null): absence means 'not
    recorded', which is exactly what every record written before this field existed means."""
    import datetime
    devs, ok, failed = [], 0, 0
    for serial, res in (results or {}).items():
        status = res[0] if isinstance(res, (tuple, list)) and res else res
        detail = res[1] if isinstance(res, (tuple, list)) and len(res) > 1 else ""
        e = {"serial": serial, "status": status}
        if status == "ok":
            ok += 1
        elif status in ("fail", "error"):
            failed += 1
            if detail:
                e["error"] = detail                        # the WHY — only on a failure
        devs.append(e)
    if not any(d["status"] in ("ok", "fail", "error") for d in devs):
        return                                              # only skips/cancels -> nothing worth a record
    rec = {"when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "action": action, "ok": ok, "failed": failed, "devices": devs}
    if elapsed is not None:                                  # 0.0 is a real measurement, not "unknown"
        rec["total_secs"] = round(elapsed, 1)
    _append_history(root, f"{action}-history", rec, log,
                    summary=f"{action} run logged: {ok} ok, {failed} failed")


def log_save_fail(root, name, serial, error, log=print):
    """Record a FAILED Save to save-history (successful saves are logged by capture_to_pc). Shape stays
    compatible with the success record — plus status='fail' and the reason — so the history viewer shows
    WHY a capture failed on that device+run."""
    import datetime
    _append_history(root, "save-history", {
        "when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "profile": name, "serial": serial, "status": "fail", "error": error,
    }, log, summary=f"save FAILED: {name} — {error}")


def _log_download_run(root, results, elapsed, log=print):
    """Append ONE whole-Download record to the per-machine download-history.<machine>.jsonl. Captures the
    run's TOTAL length (bytes + seconds) and every device + its profile."""
    import datetime
    devs, total = [], 0
    for serial, res in results.items():
        status = res[0] if isinstance(res, (tuple, list)) and res else res
        detail = res[1] if isinstance(res, (tuple, list)) and len(res) > 1 else ""
        e = {"serial": serial, "status": status}
        if status == "ok":
            e["profile"] = detail
            try:
                e["bytes"] = P.Profile(pathlib.Path(root) / detail).golden_size()
            except Exception:
                e["bytes"] = 0
            total += e["bytes"]
        elif status in ("fail", "error") and detail:
            e["error"] = detail                             # the reason (provision() already captured it)
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
    _append_history(root, "download-history", rec, log,
                    summary=(f"download run logged: {len(devs)} device(s), "
                             f"{total // 1048576} MB total in {rec['total_secs']:.0f}s"))


def seed_default_manifest(pdir, name):
    """Seed the per-profile Download manifest from the captured golden's pkglist — every captured app,
    both axes (APK + Config) on. Used after the first capture so Download has apps to restore. The behavior
    defaults FOLLOW what the operator chose in the Save modal (the capture-manifest's @flags), so a Save
    selection pre-fills Download; any flag absent from the capture-manifest defaults on (full restore)."""
    pdir = pathlib.Path(pdir)
    man = pdir / "manifest"
    if man.exists() and P.manifest_pkgs(man):           # operator already has a real selection — keep it
        return
    pl = pdir / "golden_root_payload" / "pkglist.txt"
    apps = [a.strip() for a in pl.read_text().splitlines() if a.strip()] if pl.exists() else []
    cap = P.manifest_flags(pdir / "capture-manifest")   # the Save modal's behavior choices (if any)
    flags = {fl: cap.get(fl, "on")
             for fl in ("settings", "hardening", "grants", "homescreen", "gamelauncher", "wifi")}
    P.save_manifest(man, apps, flags, header=f"# {name} default manifest")


def capture_to_pc(adb, name, stamp, root="profiles", log=print, dry_pull=False):
    """Capture the connected golden into profiles/<name>/. The existing (good) profile is touched
    ONLY after the new payload is pulled AND verified — a failed capture/pull never destroys it."""
    pdir = pathlib.Path(root) / name
    cap_man = pdir / "capture-manifest"
    dest = pdir / "golden_root_payload"
    t0 = time.monotonic()
    # Root preflight: Save clones the golden's ROOT payload (per-app data/BIOS/settings), which needs root.
    # Fail FAST here instead of pushing scripts and then hanging on the su capture — so a non-rooted unit
    # is caught immediately, not after a long doomed capture. (dry_pull is the no-device test path.)
    if not dry_pull and not adb.is_root():
        log("not rooted — run '⓪ Root' first. Save needs root to clone the golden's payload; "
            "aborting now instead of after a long capture. Unit untouched.")
        return False
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
        # Pull as ONE archive, never `adb pull <dir>`: a directory pull can silently drop files on Windows
        # (mirror of the push bug) and quietly write an incomplete golden. _pull_dir packs on-device, pulls
        # a single file (with the '[ NN%]' progress bar), and unpacks on the PC — identical on every OS.
        log("pulling the captured payload to the PC (one archive — a multi-GB golden can take several minutes)...")
        if not _pull_dir(adb, TMPCAP, incoming, log):
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
    if not dry_pull:                                        # log the Save to the per-machine save-history
        import datetime
        b = P.Profile(pdir).golden_size()
        _append_history(root, "save-history", {
            "when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "profile": name,
            "serial": getattr(adb, "serial", None),
            "bytes": b,
            "secs": round(time.monotonic() - t0, 1),
        }, log, summary=f"save logged: {name} ({b // 1048576} MB)")
    log(f"==> captured golden into profiles/{name} (prev kept for rollback)")
    return True


def fastboot_missing_help():
    """Actionable guidance when a unit reboots to its bootloader but never shows in `fastboot devices`.

    On Windows this is almost always the missing BOOTLOADER USB driver, NOT a code/device fault: adb and
    fastboot use DIFFERENT Windows drivers, so Download + the on-device Magisk patch work while the fastboot
    FLASH can't see the unit. Linux gets this for free via udev, which is why the same unit roots there. The
    fix is a one-time per-PC WinUSB driver install (setup-windows.bat). On POSIX a timeout here is a
    cable/mode issue instead."""
    if os.name == "nt":
        return ("ERROR: the unit reached its bootloader but Windows can't see it in fastboot — the "
                "BOOTLOADER USB DRIVER is missing. (adb uses a different driver, so Download and the Magisk "
                "patch worked; only the fastboot FLASH is blocked. Linux roots this unit fine because udev "
                "provides the driver automatically.) ONE-TIME FIX per PC — run scripts\\setup-windows.bat "
                "(Administrator): it installs the fastboot WinUSB driver into the Windows driver store, so "
                "this AND every future unit auto-binds on plug — no Zadig, no per-device setup. Then "
                "unplug/replug and re-run Root. The unit is UNHARMED — hold Power ~10s to reboot it back to "
                "Android.")
    return ("ERROR: device did not enter fastboot (nothing in `fastboot devices`). Check the USB cable/port "
            "and that it reached the bootloader; on Linux make sure android-udev rules are installed. The "
            "unit is unharmed and still bootable.")


def fastboot_flasher(fastboot, wait=True, on_critical=None):
    """Flash backend: write the patched image to `target` via fastboot, trying FASTBOOTD (userspace fastboot)
    FIRST and falling back to BOOTLOADER fastboot. Rationale: some unlocked bootloaders reject `flash`
    ("Flashing is not allowed") while their userspace fastbootd accepts it (e.g. Retroid); other/older units
    have no fastbootd and only the bootloader can flash. Works on units whose fastboot implements `flash`
    (Retroid/AYN/Odin). Returns callable(adb, target, image, log)->bool; always reboots back to the OS, never
    strands the unit in fastboot. `on_critical(bool)` brackets the actual write so the GUI can warn before a
    Cancel that could brick."""
    def _bracketed_flash(target, image):
        if on_critical:
            on_critical(True)
        try:
            return fastboot.flash(target, image)
        finally:
            if on_critical:
                on_critical(False)

    def _flash(adb, target, image, log):
        # (1) Prefer fastbootd (userspace fastboot) — bootloader fastboot rejects `flash` on some units.
        log(f"rebooting to fastbootd to flash {target}...")
        adb.raw("reboot", "fastboot")
        in_fastboot = (not wait) or fastboot.wait(on_tick=lambda s: log(f"  …waiting for fastbootd ({s}s)"))
        if in_fastboot:
            if _bracketed_flash(target, image):
                fastboot.reboot()
                return True
            log("fastbootd rejected the flash; falling back to bootloader fastboot...")
            fastboot.reboot_bootloader()               # already in a fastboot mode -> safe hop to bootloader
        else:
            log("no fastbootd on this unit; falling back to bootloader fastboot...")
            adb.raw("reboot", "bootloader")            # never reached fastboot -> assume still in the OS

        # (2) Fall back to BOOTLOADER fastboot.
        if wait and not fastboot.wait(on_tick=lambda s: log(f"  …waiting for fastboot ({s}s)")):
            log(fastboot_missing_help())
            return False
        if _bracketed_flash(target, image):
            fastboot.reboot()
            return True
        log("ERROR: patched flash failed in both fastbootd and bootloader fastboot — this unit's fastboot may "
            "not support 'flash' (e.g. MANGMI → needs the EDL backend). Booting back to the OS, NOT rooted.")
        fastboot.reboot()                              # never strand the unit in fastboot
        return False
    return _flash


def edl_flasher(edl, geometry, wait=True, on_critical=None):
    """Flash backend using Qualcomm EDL / Firehose — for units whose bootloader fastboot can't write
    (MANGMI). Reboots to EDL, Firehose-writes the patched image to `target` at the firmware `geometry`
    (init_boot_<slot> sector/LUN), then resets. Returns callable(adb, target, image, log)->bool.
    `on_critical(bool)` brackets the Firehose write for the GUI's Cancel brick-warning."""
    def _flash(adb, target, image, log):
        log(f"rebooting to EDL to flash {target} (Firehose)...")
        adb.raw("reboot", "edl")
        _edl_port_kind = "COM port (QDLoader 9008)" if os.name == "nt" else "serial /dev/ttyUSB"
        port = edl.find_port(timeout=(60 if wait else 4),
                             on_tick=lambda s: log(f"  …waiting for EDL {_edl_port_kind} ({s}s)"))
        if not port:
            drv = ("QDLoader 9008 driver installed?" if os.name == "nt" else "qcserial driver?")
            log(f"ERROR: no EDL port became usable ({drv} hold power ~12s to recover).")
            return False
        with tempfile.TemporaryDirectory() as td:
            if on_critical:
                on_critical(True)
            try:
                ok = edl.flash_partition(port, target, image, geometry, td, log=log)
            finally:
                if on_critical:
                    on_critical(False)
            # A SUCCESSFUL write already reboots the unit out of EDL via the <power value="reset"> in the
            # rawprogram. reset() still runs as a safety net (a hung/killed write may not have reached that
            # tag) so a failed flash never strands the unit on a black EDL screen — but only warn when it is
            # genuinely stranded: the flash FAILED and the fallback reset couldn't reboot it either.
            rebooted = edl.reset(port, td, log=log)
            if not rebooted and not ok:
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
    # Fail fast on a host/tool OS mismatch BEFORE rebooting the unit to EDL (so it isn't stranded there).
    # On Windows the host tools must be the .exe builds — a Linux ELF makes subprocess raise
    # "WinError 193: %1 is not a valid Win32 application", which the port-open branch would otherwise
    # misreport as a missing QDLoader driver (the driver is fine; the tool is the wrong OS).
    if os.name == "nt" and not (str(q).lower().endswith(".exe") and str(f).lower().endswith(".exe")):
        return None, ("EDL flashing on Windows needs the Windows host tools QSaharaServer.exe + "
                      "fh_loader.exe (from Qualcomm QPST/QFIL) in the firmware payload — this build ships "
                      "only the Linux binaries, which Windows can't execute. Fix: install QPST, then run "
                      "scripts/install-edl-host-tools.ps1 (fans both .exe into every EDL payload), or drop "
                      "both .exe beside the Linux tools by hand and retry (the 9008 driver/COM port are fine).")
    edl = Edl(q, f, p, runner=(runner or subprocess_runner), cancel=getattr(fastboot, "cancel", None))
    return edl_flasher(edl, geom, on_critical=on_critical), None


def _img_kernel_size(path):
    """kernel_size from an Android boot image header (magic 'ANDROID!', then a LE u32 at offset 8 — the
    field is at the same offset in header versions v0–v4). Returns the size, or None if `path` isn't an
    Android boot image. An init_boot is ramdisk-only, so its kernel_size is 0; a full boot.img is >0.
    Used to refuse an image whose type doesn't match the flash target (which otherwise bricks the unit)."""
    try:
        with open(path, "rb") as fh:
            hdr = fh.read(12)
    except OSError:
        return None
    if len(hdr) < 12 or hdr[:8] != b"ANDROID!":
        return None
    return int.from_bytes(hdr[8:12], "little")


def root(adb, fastboot, stock_init_boot, magisk_apk=None, log=print, wait=True, model_match=None,
         force=False, flasher=None, capture_store=None):
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
        if not P.model_matches(model_match, model):
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

    # IMAGE/PARTITION-TYPE GUARD (model-independent, so it fires even when a profile sets no model_match):
    # an init_boot image is RAMDISK-ONLY (kernel_size 0). Flashing it to a plain `boot` partition — the
    # target on pre-init_boot units like the Retroid Pocket 5 / Odin (kona) — removes the kernel and the
    # unit bootloops straight to fastboot ("keeps returning to fastboot on Start"). The inverse (a full
    # boot.img with a kernel flashed to init_boot) is wrong too. Refuse BEFORE touching the unit — NOT
    # bypassed by `force` (this is a physical image mismatch, unrelated to which model sibling it is).
    part = target[:-2] if target.endswith(("_a", "_b")) else target
    ksz = _img_kernel_size(stock)
    if ksz is None:
        # Not a recognizable Android boot image — can't type-check it here. Not fatal: the on-device
        # boot_patch.sh needs a real boot image and fails loudly on anything else, so this never flashes.
        log(f"warning: '{pathlib.Path(stock).name}' has no 'ANDROID!' boot header — skipping the "
            "image/partition-type check (the on-device Magisk patch will reject a non-boot image).")
    elif part == "boot" and ksz == 0:
        log(f"REFUSING: '{pathlib.Path(stock).name}' is an init_boot (ramdisk-only, no kernel) but this "
            f"unit flashes to '{target}', which needs a FULL boot image. Flashing it would remove the "
            "kernel and bootloop the unit to fastboot. Use THIS unit's own stock boot.img (wrong-device "
            "image — e.g. an Odin2 init_boot on a Retroid Pocket 5).")
        return False
    elif part == "init_boot" and ksz != 0:
        log(f"REFUSING: '{pathlib.Path(stock).name}' is a full boot image (has a kernel) but this unit "
            f"flashes to '{target}' (init_boot is ramdisk-only). Wrong image would brick boot. Use the "
            "unit's stock init_boot.")
        return False

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
    if wait and not adb.wait_boot(on_tick=lambda s, st: log(boot_tick_msg(s, st))):
        log("ERROR: unit did not boot after the root flash — investigate before retrying.")
        return False
    log("device booted.")

    # (4) verify adb-shell root. The overlay.d boot-grant service fires on the SAME sys.boot_completed=1
    # this wait rode and needs a beat to write the shell-allow policy (it waits for magiskd first), so
    # give it a bounded grace window before deciding it didn't take — otherwise we check a hair too early
    # and drop to the screen-dependent auto-tap even while the zero-touch grant is landing.
    if not wait:
        return True
    from . import config as _cfg
    granted = _await_boot_grant(adb, log=log) if _cfg.bake_boot_grant() else adb.is_root()
    if granted:
        if capture_store:
            capture_factory_init_boot(adb, capture_store, log=log)
        log("✓ ROOTED — shell pre-authorized at boot (zero-touch, no Magisk prompt). "
            "Ready to '② Download to selected device'.")
        return True
    if _cfg.auto_grant_shell():
        log("shell not granted by the boot-grant — auto-granting via the on-device Magisk prompt…")
        if grant_shell_root(adb, log=log):
            if capture_store:
                capture_factory_init_boot(adb, capture_store, log=log)
            log("✓ ROOTED — shell auto-granted. Ready to '② Download'.")
            return True
        return False                       # grant_shell_root already logged the manual fallback
    log("init_boot flashed + Magisk installed, but the adb shell uid isn't granted root YET. One-time per "
        "unit: on the device open Magisk → Superuser → enable the 'Shell' / '[SharedUID] Shell' toggle, "
        "then retry. (MagiskSU gates the shell uid until you allow it.)")
    return False


def _persist_grant(adb, log=print):
    """Make the just-obtained shell grant permanent (magisk policy) + set global root access, by
    running the bundled grant-persist.sh AS ROOT. Returns True if the shell policy read-back = allow.
    A False here means the shell is rooted NOW but may re-prompt after a reboot — not fatal."""
    if not adb.push(str(GRANT_PERSIST), DEV_GRANT):
        log("  ⚠ could not push grant-persist.sh — shell is rooted now but may re-prompt after reboot.")
        return False
    rc, out, err = adb.su(f"sh {DEV_GRANT}", timeout=30)
    adb.shell(f"rm -f {DEV_GRANT}")
    if "CAS_GRANT policy=2" in out:
        log("  ✓ shell root made permanent (magisk policy: shell uid 2000 = allow).")
        return True
    log(f"  ⚠ persistence unconfirmed (rc={rc}): {((out or err) or '').strip()[:160]} — shell is "
        "rooted now but may re-prompt after reboot.")
    return False


def grant_shell_root(adb, log=print, attempts=3, ui_timeout=15):
    """Zero-touch: obtain + persist the MagiskSU shell grant with no human tap. Raises the on-device
    Magisk Superuser prompt (device-side-backgrounded `su`, so the PC never blocks), auto-taps
    'Grant' via uiautomator (gated to the Magisk app so we never mis-tap), confirms root with a short
    re-check, then makes it permanent. Returns True once the shell holds root; on failure logs the
    one-time manual instruction and returns False."""
    from . import uiauto
    from .adb import SU
    if "uid=0" in adb.su("id", timeout=8)[1]:        # already granted (e.g. a remembered policy)
        _persist_grant(adb, log)
        return True
    for i in range(attempts):
        log(f"  auto-grant {i + 1}/{attempts}: raising the Magisk Superuser prompt…")
        # The unit reboots to a locked/asleep screen; wake it and slide past a NON-secure keyguard first
        # or uiautomator can't see/tap the Grant dialog — the exact reason the auto-tap silently missed
        # and root had to be granted by hand. Both are idempotent / no-ops on an already-awake unit.
        adb.shell("input keyevent 224")               # KEYCODE_WAKEUP — turn the screen on
        adb.shell("wm dismiss-keyguard")              # slide past a swipe/none lock (no-op on a secure PIN)
        adb.shell(f"{SU} -c id >/dev/null 2>&1 &")    # device-side background: returns immediately
        for _ in range(ui_timeout):
            if MAGISK_PKG in uiauto.foreground(adb) and uiauto.tap(adb, GRANT_PROMPT_BTN):
                break
            time.sleep(1)
        # Re-check unconditionally: a grant can land even when this iteration's tap missed the
        # button, and a prior grant may only register now (no fresh prompt once the policy exists).
        if "uid=0" in adb.su("id", timeout=8)[1]:
            log("  ✓ shell auto-granted.")
            _persist_grant(adb, log)
            return True
        log("  prompt not answered yet — retrying." if i + 1 < attempts else "  auto-grant failed.")
    log("init_boot flashed + Magisk installed, but the shell uid could NOT be auto-granted. One-time "
        "per unit: on the device open Magisk → Superuser → enable the 'Shell' / '[SharedUID] Shell' "
        "toggle, then retry. (MagiskSU gates the shell uid until you allow it.)")
    return False


def resolve_seal_stock(library_stock, capture_path, fingerprint, log=print):
    """Pick the init_boot seal() will flash to un-root: prefer this unit's OWN captured factory image
    (exact-build → its device OTA source-verifies), else fall back to the model-matched library image
    with a LOUD warning that the unit's OTA may break until it's re-captured."""
    if capture_path:
        log(f"  un-root: restoring this unit's own captured factory init_boot for build {fingerprint} "
            "(keeps its device OTA healthy).")
        return str(capture_path)
    log(f"  ⚠ no factory init_boot captured for build {fingerprint} — sealing with the library image; "
        "this unit's device OTA may fail (code 20) until it's rooted from a clean state to capture it.")
    return library_stock


def seal(adb, fastboot, stock_init_boot, log=print, wait=True, model_match=None, force=False, flasher=None):
    """Make a provisioned unit RETAIL-READY (run AFTER provision + verify):
      1) check the stock init_boot matches THIS device model (wrong-model flash bricks boot)
      2) uninstall the Magisk app (needs root)
      3) un-root by flashing STOCK init_boot, then CONFIRM root is actually gone
      4) hide Developer Options + disable USB debugging LAST (drops adb) — after confirmed boot + un-root
    Never strands the unit in fastboot, and never disables adb on an unverified/failed seal.
    force=True proceeds on a model MISMATCH with a loud warning instead of refusing."""
    log("SEAL: locking the unit down for retail.")
    # Upfront heads-up (option b): if the unit reports not-rooted, say so IMMEDIATELY — it may already be
    # sealed, or ⓪ Root / ② Download were skipped. Seal still flashes stock to GUARANTEE un-root (the
    # flaky-su-grant safety), so this is a warning, not an abort — the operator can cancel if it wasn't
    # provisioned rather than discover it after the ~2-3 min flash.
    if not adb.is_root():
        log("⚠ device reports NOT rooted — it may ALREADY be sealed, or ⓪ Root / ② Download were skipped. "
            "Sealing will still flash stock to guarantee un-root (~2-3 min); the ship-clean scrub is skipped "
            "(needs root). Cancel now and run Root + Download first if this unit wasn't provisioned.")
    flasher = flasher or fastboot_flasher(fastboot, wait=wait)   # brand-agnostic: caller passes edl_flasher for EDL units

    # NEVER seal/un-root the GOLDEN. is_golden() needs root to read the marker, so only check when rooted
    # (the golden ships rooted). This protects the master from "Apply to ALL + Lock".
    if adb.is_root() and adb.is_golden():
        log("REFUSING: this device carries the golden lock (.cas_golden) — will not un-root the master.")
        return False

    # (1) model cross-check — flashing another model's init_boot bricks boot.
    if model_match:
        model = adb.getprop("ro.product.model")
        if not P.model_matches(model_match, model):
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
        if not adb.wait_boot(on_tick=lambda s, st: log(boot_tick_msg(s, st))):
            log("ERROR: unit did not boot after un-root flash — NOT disabling USB debugging. Investigate.")
            return False
        if adb.is_root():
            log("ERROR: still ROOTED after stock flash (wrong slot / no-op flash?) — NOT sealing. "
                "adb left enabled so you can retry.")
            return False
        log("confirmed un-rooted.")
    # (4) LAST retail lockdown — the inverse of what Root/the operator opened: HIDE Developer Options, turn
    # the OEM-unlocking toggle back OFF, THEN disable USB debugging (which drops adb). These run as the shell
    # uid (has WRITE_SECURE_SETTINGS) so they work WITHOUT root and even post-un-root — a flaky su grant can
    # never leave Developer Options visible on a shipped unit. One call, adb_enabled LAST.
    # `;` (never `&&`) so a rejected setting can't stop adb_enabled from running.
    # SCOPE: this clears the toggle the operator flipped to unlock; it does NOT re-lock the bootloader. Root
    # never unlocked it (unlocked is a PRECONDITION — these units ship unlocked), so re-locking would close
    # something we never opened, and on Qualcomm it mandates a userdata wipe that would erase the golden.
    # The authoritative unlock-ability lives in the persistent data block (frp), which the shell uid cannot
    # write — so this removes the affordance, it is not a bootloader lock. EDL (9008) is PBL-level and has
    # no software lock at all, by design.
    adb.shell("settings put global development_settings_enabled 0; "
              "settings put secure development_settings_enabled 0; "
              "settings put global oem_unlock_allowed 0; "
              "settings put global adb_enabled 0")
    log("hid Developer options + cleared the OEM-unlock toggle + disabled USB debugging. Device is SEALED "
        "— adb will now disconnect. Done.")
    return True


def root_all(make_adb, make_fb, devices, profiles_root="profiles", appdir=None, log=print, profile=None,
             profile_map=None, force_serials=None, parallel=True, on_critical=None):
    """Batch ROOT: every connected 'device'-state unit, in PARALLEL by default (all units reboot/flash at
    once — the big win, since root is reboot-dominated). Profile per device: profile_map[serial] > `profile`
    > auto-match by model. force_serials = serials to flash even on a model mismatch (a deliberate, already-
    confirmed assignment). Devices with no profile / no stock_init_boot / the golden are skipped. Returns
    {serial: (status, detail)}, failures isolated.
    (param is profiles_root, NOT root, so it can't shadow the root() function called below.)"""
    t0 = time.monotonic()
    appdir = pathlib.Path(appdir) if appdir else pathlib.Path(".")
    force_serials = force_serials or set()

    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        adb = fb = None                       # bound before the try so the except handler can probe safely
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
            from . import firmware as FW
            flasher = None
            proven = None                     # (identity, firmware_id, version) once firmware resolves
            phase = "fastboot_flash"          # coarse recovery hint; the EDL branch below flips it
            try:
                idn = FW.identity(adb)
                fwres = FW.resolve(serial, idn, FW.firmware_root())
                fw = fwres.get("firmware")
                proven = (idn, fwres.get("firmware_id"), fwres.get("version"))
                if fw is None and fwres.get("firmware_id") != FW.DEFAULT_FW_ID and FW.edl_only_device(idn):
                    # EDL-only unit (MANGMI): fastboot can't write init_boot, so a fallback flash is doomed.
                    # Fail-fast with the fix instead of rebooting to a bootloader flash that can only fail.
                    msg = ("EDL-only unit (e.g. MANGMI) but no firmware build resolved — add its build under "
                           "_firmware/. Not attempting a fastboot flash the bootloader can't perform.")
                    log(f"[{serial}] {msg}")
                    return ("fail", msg)
                if fw is not None:
                    sb = fw.stock_boot_image(fwres.get("version"))
                    if sb:
                        stock_path = str(sb)            # the unit's own init_boot from its firmware build
                    if fw.flash_method == "edl":
                        phase = "edl_flash"
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
            msgs = []                                      # capture the reason a FAIL surfaces (like warmup/download)

            def _wlog(m, s=serial):
                msgs.append(m)
                log(f"[{s}] {m}")
            capture_store = _ibs.store_root(FW.firmware_root())
            ok = root(adb, fb, stock_path,
                      magisk_apk=_kit_apk(MAGISK_PKG, prof, appdir, magisk_rel),
                      log=_wlog,
                      model_match=prof.meta.get("model_match"), force=(serial in force_serials),
                      flasher=flasher, capture_store=capture_store)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", prof.name)
            if ok:
                if proven and proven[1]:
                    # Root returned ok, which means the unit BOOTED — record the combination that
                    # worked. Evidence only; nothing gates on it.
                    FW.log_proven_pair(*proven)
                return ("ok", prof.name)
            return _fail_with_recovery("root", phase, adb, fb, "fail",
                                       msgs[-1] if msgs else prof.name, _wlog)
        except Exception as e:
            log(f"[{serial}] ERROR: {e}")
            return _fail_with_recovery("root", "", adb, fb, "error", str(e), log)
    results = _each_device(devices, worker, parallel)
    log_run(profiles_root, "root", results, log, elapsed=time.monotonic() - t0)   # + BATCH wall-clock
    return results


def seal_all(make_adb, make_fb, devices, profiles_root="profiles", appdir=None, log=print, profile=None,
             profile_map=None, force_serials=None, parallel=True, on_critical=None):
    """Batch SEAL: every connected 'device'-state unit, in PARALLEL by default (each un-roots + reboots at
    once, mirroring root_all). Profile per device: profile_map[serial] > `profile` > auto-match by model.
    force_serials = serials to seal even on a model mismatch (deliberate, already-confirmed). The golden and
    devices with no profile / no stock_init_boot are skipped. Per-device isolated. Returns {serial: (status,
    detail)}."""
    t0 = time.monotonic()
    appdir = pathlib.Path(appdir) if appdir else pathlib.Path(".")
    force_serials = force_serials or set()

    def worker(serial, state):
        if state != "device":
            log(f"[{serial}] skip (state={state})")
            return ("skip", state)
        adb = fb = None                       # bound before the try so the except handler can probe safely
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
            phase = "fastboot_flash"          # coarse recovery hint; the EDL branch below flips it
            try:
                from . import firmware as FW
                idn = FW.identity(adb)
                fwres = FW.resolve(serial, idn, FW.firmware_root())
                fw = fwres.get("firmware")
                if fw is None and fwres.get("firmware_id") != FW.DEFAULT_FW_ID and FW.edl_only_device(idn):
                    # EDL-only unit (MANGMI): fastboot can't write init_boot, so a fallback flash is doomed.
                    # Fail-fast with the fix instead of rebooting to a bootloader flash that can only fail.
                    msg = ("EDL-only unit (e.g. MANGMI) but no firmware build resolved — add its build under "
                           "_firmware/. Not attempting a fastboot flash the bootloader can't perform.")
                    log(f"[{serial}] {msg}")
                    return ("fail", msg)
                if fw is not None:
                    sb = fw.stock_boot_image(fwres.get("version"))
                    if sb:
                        stock_path = str(sb)
                    if fw.flash_method == "edl":
                        phase = "edl_flash"
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
            msgs = []                                      # capture the reason a FAIL surfaces (like warmup/download)

            def _wlog(m, s=serial):
                msgs.append(m)
                log(f"[{s}] {m}")
            # Prefer this unit's OWN captured factory init_boot (exact build) over the model-matched
            # library image, so the sealed unit's device OTA source-verifies. Falls back + warns — but
            # ONLY for capture-APPLICABLE units. capture_factory_init_boot only ever populates the store
            # when there's a distinct inactive A/B slot to read (its own slot gate); an A-only unit (empty
            # slot suffix) can NEVER have a capture, so running this lookup there would just emit a
            # permanent false "OTA may fail (code 20)" warning on every seal. Skip it for those units —
            # use the library-resolved stock_path with no warning.
            if adb.slot_suffix() in ("_a", "_b"):
                store_root = _ibs.store_root(FW.firmware_root())
                _fp = adb.getprop("ro.build.fingerprint")
                stock_path = resolve_seal_stock(stock_path, _ibs.get(store_root, _fp), _fp, log=_wlog)
            ok = seal(adb, fb, stock_path,
                      log=_wlog,
                      model_match=prof.meta.get("model_match"), force=(serial in force_serials),
                      flasher=flasher)
            if adb.cancel is not None and adb.cancel.is_set():
                return ("cancelled", prof.name)
            if ok:
                return ("ok", prof.name)
            if any("SEALED" in m for m in msgs):
                # seal() logged its completion marker, then adb dropped BY DESIGN — this is success, not a
                # failure; never raise the attention popup for a unit that actually sealed.
                rec = RC.advise("lock", "done", RC.DeviceMode.SEALED_OK)
                return ("ok", "sealed (adb dropped after the seal completed)", rec)
            return _fail_with_recovery("lock", phase, adb, fb, "fail",
                                       msgs[-1] if msgs else prof.name, _wlog)
        except Exception as e:
            log(f"[{serial}] ERROR: {e}")
            return _fail_with_recovery("lock", "", adb, fb, "error", str(e), log)
    results = _each_device(devices, worker, parallel)
    log_run(profiles_root, "lock", results, log, elapsed=time.monotonic() - t0)   # + BATCH wall-clock
    return results
