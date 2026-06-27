"""Headless CLI for scripted/batch provisioning (the GUI's engine, no window). Usage:
  python -m cas.cli list
  python -m cas.cli provision      [--profile NAME] [--serial S]
  python -m cas.cli provision-all
  python -m cas.cli capture NAME   [--serial S]
  python -m cas.cli seal           [--profile NAME] [--serial S]
Global: --adb PATH  --fastboot PATH  (point at windows-kit\\adb.exe / fastboot.exe on Windows).
"""
import argparse
import datetime
import pathlib
import sys

from . import APPDIR, find_adb
from . import profiles as P
from . import provision as PV
from .adb import Adb, Fastboot, list_devices
from .config import library_root, es_media_src


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _resolve_profile(adb, name, proot):
    """Explicit --profile NAME, else auto-match the device's ro.product.model."""
    if name:
        d = pathlib.Path(proot) / name
        return P.Profile(d) if (d / "profile.meta").exists() else None
    return P.match_profile(adb.getprop("ro.product.model"), proot)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cas.cli", description="Console Auto Setup — CLI")
    # Default to None so an explicit --adb/--fastboot override always wins over the sibling
    # platform-tools auto-detect (find_adb) applied below.
    ap.add_argument("--adb", default=None)
    ap.add_argument("--fastboot", default=None)
    ap.add_argument("--serial", default=None)
    ap.add_argument("--library", default=None,
                    help="profile-library path (default: cas-config.json / CAS_PROFILES / APPDIR/profiles)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list profiles")
    rp = sub.add_parser("root", help="root a fresh unit (flash Magisk-patched init_boot + app, from PC)")
    rp.add_argument("--profile")
    rp.add_argument("--force", action="store_true", help="proceed even if the device model != the profile")
    sub.add_parser("root-all", help="root every connected device (auto-matched, from PC)")
    pp = sub.add_parser("provision", help="provision one device"); pp.add_argument("--profile")
    sub.add_parser("provision-all", help="provision every connected device (auto-matched)")
    sub.add_parser("seal-all", help="seal every connected device (auto-matched)")
    cp = sub.add_parser("capture", help="capture a golden into a profile"); cp.add_argument("name")
    sp = sub.add_parser("seal", help="un-root + lock down a verified unit"); sp.add_argument("--profile")
    sp.add_argument("--force", action="store_true", help="proceed even if the device model != the profile")
    a = ap.parse_args(argv)
    proot = a.library or str(library_root())

    # --adb/--fastboot override wins; otherwise auto-detect APPDIR/platform-tools, falling back to PATH.
    a.adb = a.adb or find_adb("adb")
    a.fastboot = a.fastboot or find_adb("fastboot")

    if a.cmd == "list":
        for pr in P.list_profiles(proot):
            print(f"{pr.name:24} frontend={pr.meta.get('frontend','')}  match={pr.meta.get('model_match','')}")
        return 0

    if a.cmd == "provision-all":
        res = PV.provision_all(lambda s: Adb(serial=s, adb=a.adb), list_devices(adb=a.adb), root=proot,
                               es_media_src=es_media_src())
        print("batch:", ", ".join(f"{k}={v[0]}" for k, v in res.items()))
        return 0 if all(v[0] in ("ok", "skip") for v in res.values()) else 1

    if a.cmd in ("root-all", "seal-all"):
        fn = PV.root_all if a.cmd == "root-all" else PV.seal_all
        res = fn(lambda s: Adb(serial=s, adb=a.adb), lambda s: Fastboot(serial=s, fastboot=a.fastboot),
                 list_devices(adb=a.adb), profiles_root=proot, appdir=APPDIR)
        print(f"{a.cmd}:", ", ".join(f"{k}={v[0]}" for k, v in res.items()))
        return 0 if all(v[0] in ("ok", "skip") for v in res.values()) else 1

    adb = Adb(serial=a.serial, adb=a.adb)

    if a.cmd == "root":
        prof = _resolve_profile(adb, a.profile, proot)
        if not prof:
            print("no matching profile — pass --profile NAME"); return 1
        stock_rel = prof.meta.get("stock_init_boot") or PV.DEFAULT_STOCK_INIT_BOOT
        magisk_rel = prof.meta.get("magisk_apk") or PV.DEFAULT_MAGISK_APK
        fb = Fastboot(serial=a.serial, fastboot=a.fastboot)
        return 0 if PV.root(adb, fb, P.resolve_asset(prof, APPDIR, stock_rel),
                            magisk_apk=P.resolve_asset(prof, APPDIR, magisk_rel),
                            model_match=prof.meta.get("model_match"), force=a.force) else 1

    if a.cmd == "provision":
        prof = _resolve_profile(adb, a.profile, proot)
        if not prof:
            print("no matching profile — pass --profile NAME"); return 1
        return 0 if PV.provision(adb, prof, es_media_src=es_media_src()) else 1

    if a.cmd == "capture":
        return 0 if PV.capture_to_pc(adb, a.name, _stamp(), root=proot) else 1

    if a.cmd == "seal":
        prof = _resolve_profile(adb, a.profile, proot)
        stock_rel = prof.meta.get("stock_init_boot") if prof else None
        if not stock_rel:
            print("profile has no stock_init_boot set — cannot un-root"); return 1
        fb = Fastboot(serial=a.serial, fastboot=a.fastboot)
        return 0 if PV.seal(adb, fb, APPDIR / stock_rel,
                            model_match=prof.meta.get("model_match"), force=a.force) else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
