"""Unit tests for the cas package — mock adb runner, no real device. Run from project root:
    python3 -m unittest discover -s tests -p 'test_*.py' -t .
"""
import os
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import pathlib
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas.adb import Adb, Fastboot, list_devices
from cas import profiles as P
from cas import provision as PV
from cas import warnings as WARN
from cas import uiauto as UI


# --- CAS_CONFIG isolation (module-wide safety net) ------------------------------------------------
# INVARIANT: no test in this module may EVER write the operator's real cas-config.json (it lives at the
# repo root, gitignored — see cas/config.py's config_path()). A test that exercises a real (non-dry)
# provision()/provision_all() success path — or any other config.set_*()/record_download() writer —
# without CAS_CONFIG pointed at a temp file falls straight through to that real file. Individual
# TestCase classes below layer their OWN setUp()/tearDown() save-and-restore on top of this (some tests
# need a specific config content mid-test); this module-level default is the backstop that catches
# whatever a test — present or future — forgets to isolate itself.
_PREV_CAS_CONFIG = None
_MODULE_CFG_DIR = None


def setUpModule():
    global _PREV_CAS_CONFIG, _MODULE_CFG_DIR
    _PREV_CAS_CONFIG = os.environ.get("CAS_CONFIG")
    _MODULE_CFG_DIR = tempfile.mkdtemp(prefix="cas-test-config-")
    os.environ["CAS_CONFIG"] = os.path.join(_MODULE_CFG_DIR, "cas-config.json")


def tearDownModule():
    import shutil
    if _PREV_CAS_CONFIG is None:
        os.environ.pop("CAS_CONFIG", None)
    else:
        os.environ["CAS_CONFIG"] = _PREV_CAS_CONFIG
    if _MODULE_CFG_DIR:
        shutil.rmtree(_MODULE_CFG_DIR, ignore_errors=True)


class FakeRunner:
    """Records calls; returns canned (rc, out, err) shaped like the real adb."""

    def __init__(self, model="Odin2 Mini", golden=False, root=True, boot="1", sd=True,
                 push_ok=True, pull_ok=True, su_blocked=False, slot="_a", first_api="33",
                 device_owner=False, do_set_ok=True, do_restrict=True, release_clears=True,
                 restrict_in="device_policy", do_set_err=None, dev_code="",
                 sd_vols=None, sd_esde=False, sd_art=False):
        self.calls = []
        self.model, self.golden, self.root, self.boot, self.sd = model, golden, root, boot, sd
        # External /storage volume ids present on the (fake) device — modeling ANY volume-id format, not
        # just the hyphenated FAT 'XXXX-XXXX'. Defaults follow `sd`: one dashed card when sd, none when not.
        self.sd_vols = list(sd_vols) if sd_vols is not None else (["9C33-6BBD"] if sd else [])
        self.sd_esde, self.sd_art = sd_esde, sd_art     # does the card carry an ES-DE tree / box art
        # dev_code = ro.mangmi.dev.code — present ("" default = absent) only on MANGMI (EDL-only) units.
        self.dev_code = dev_code
        self.push_ok, self.pull_ok = push_ok, pull_ok
        # su_blocked models a real device whose MagiskSU grant prompt was never tapped: EVERY `su` call
        # hangs until the runner's timeout fires, which subprocess_runner reports as (124, "", "timeout…").
        self.su_blocked = su_blocked
        # slot = ro.boot.slot_suffix ('_a'/'_b', or '' for A-only); first_api = ro.product.first_api_level
        # (>=33 means the unit LAUNCHED on Android 13+ and so has an init_boot partition). Defaults model an
        # A/B A13 unit on slot A — so the detected flash target stays 'init_boot_a' (existing assertions hold).
        self.slot, self.first_api = slot, first_api
        self._owner = device_owner          # current device-owner state (mutated by a release broadcast)
        self.do_set_ok, self.do_restrict, self.release_clears = do_set_ok, do_restrict, release_clears
        self.do_set_err = do_set_err        # override the failure stderr (e.g. an 'Unknown admin' error)
        # which dumpsys surfaces the applied DO restrictions: 'device_policy' (older Android) or 'user'
        # (Android 14+ keeps the per-admin device_policy userRestrictions field EMPTY and lists them under
        # dumpsys user 'Effective/global restrictions' instead). Only matters when do_restrict is True.
        self.restrict_in = restrict_in

    def _storage_ls(self, cmd):
        """Emulate the device's /storage volume listing for the app's probes. Returns (rc,out,err),
        or None when `cmd` is not a storage probe. 'emulated'/'self' are always present (internal);
        `self.sd_vols` are the external cards, in WHATEVER volume-id format."""
        if "/storage/*/ES-DE" in cmd or "downloaded_media" in cmd:      # _probe_sd_media box-art scan
            hits = []
            for n in self.sd_vols:
                if self.sd_esde:
                    hits.append(f"/storage/{n}/ES-DE")
                if self.sd_art:
                    hits.append(f"/storage/{n}/downloaded_media")
            return 0, ("\n".join(hits) + "\n" if hits else ""), ""
        names = ["emulated", "self"] + list(self.sd_vols)
        if "/storage/*/" in cmd:                                        # format-agnostic volume probe
            return 0, "".join(f"/storage/{n}/\n" for n in names), ""
        if "/storage/*-*" in cmd:                                       # legacy dash-only glob (pre-fix)
            return 0, "".join(f"/storage/{n}\n" for n in names if "-" in n), ""
        return None

    def __call__(self, args, input_text=None, timeout=900):
        self.calls.append(list(args))
        if args[-1] == "devices":
            return 0, "List of devices attached\nABC123\tdevice\nDEF456\tunauthorized\n", ""
        if args[-1] == "reboot":
            return 0, "", ""
        if args[-1] == "get-state":                 # a connected device -> reachable
            return 0, "device\n", ""
        if "push" in args:
            return (0 if self.push_ok else 1), "", ""
        if "pull" in args:
            return (0 if self.pull_ok else 1), "", ""
        if "shell" in args:
            if "/debug_ramdisk/su" in args:
                if self.su_blocked:
                    return 124, "", f"timeout after {timeout}s"
                cmd = args[-1]
                if cmd == "id":
                    return (0, "uid=0(root)\n", "") if self.root else (1, "", "Permission denied")
                if ".cas_golden" in cmd:
                    return 0, ("CAS_GOLD\n" if self.golden else "CAS_NOTGOLD\n"), ""
                if "restore.sh" in cmd:
                    return 0, "[ok] restored apps\n[ok] RESTORE complete\n", ""
                if "capture.sh" in cmd:
                    return 0, "[ok] GOLDEN captured\n", ""
                if "CAS_TOK" in cmd:                    # _pull_dir device-side tar-pack sentinel
                    return 0, "CAS_TOK\n", ""
                sl = self._storage_ls(cmd)
                if sl is not None:
                    return sl
                return 0, "", ""
            tail = args[-1]
            sl = self._storage_ls(tail)
            if sl is not None:
                return sl
            if tail.startswith("dpm list-owners"):
                return 0, ("Device owner: com.gamecove.gamecove_companion\n" if self._owner
                           else "No device owner.\n"), ""
            if tail.startswith("dpm set-device-owner"):
                if self.do_set_ok:
                    self._owner = True
                    return 0, "Success: Device owner set to package\n", ""
                return 255, "", (self.do_set_err
                                 or "java.lang.IllegalStateException: Not allowed to set the device owner\n")
            if tail.startswith("dumpsys device_policy"):
                shown = self._owner and self.do_restrict and self.restrict_in == "device_policy"
                return 0, ("no_factory_reset no_safe_boot\n" if shown else "\n"), ""
            if tail.startswith("dumpsys user"):
                shown = self._owner and self.do_restrict and self.restrict_in == "user"
                return 0, ("  Effective restrictions:\n    no_factory_reset\n    no_safe_boot\n"
                           if shown else "  Effective restrictions:\n"), ""
            if tail.startswith("am broadcast") and "action.RELEASE" in tail:
                if self.release_clears and "gc-release-7f3a9c2e" in tail:
                    self._owner = False
                return 0, "Broadcast completed: result=0\n", ""
            if tail.startswith("am start"):
                return 0, "Starting: Intent\n", ""
            if "boot_patch.sh" in tail:                 # on-device Magisk patch -> stdout sentinel
                return 0, "- Patching ramdisk\n- Repacking boot image\nCAS_PATCH_OK\n", ""
            if "CAS_INJECT_OK" in tail:                 # overlay.d inject chain -> stdout sentinel
                return 0, "- Repacking boot image\nCAS_INJECT_OK\n", ""
            if tail.startswith("getprop"):
                key = tail.split()[-1]
                val = {"ro.product.model": self.model, "sys.boot_completed": self.boot,
                       "ro.boot.slot_suffix": self.slot,
                       "ro.mangmi.dev.code": self.dev_code,
                       "ro.product.first_api_level": self.first_api}.get(key, "")
                return 0, val + "\n", ""
            if "CAS_XOK" in tail:                       # box-art tar unpack confirmation sentinel
                return 0, "CAS_XOK\n", ""
            return 0, "", ""
        return 0, "", ""

    def cmds(self):
        return [" ".join(c) for c in self.calls]


GRANT_XML = (
    "<hierarchy rotation=\"0\">"
    "<node text=\"Superuser Request\" bounds=\"[0,100][1080,220]\" />"
    "<node text=\"Deny\" bounds=\"[0,900][540,1010]\" />"
    "<node text=\"Grant\" bounds=\"[540,900][1080,1010]\" />"
    "</hierarchy>")


class GrantRunner(FakeRunner):
    """Models the causal chain: raising the prompt shows a Magisk 'Grant' dialog; an `input tap`
    grants root; thereafter `su id` reports uid=0. `never_grants=True` models a prompt that never
    resolves (auto-tap fails -> manual fallback)."""

    def __init__(self, never_grants=False, wrong_foreground=False, **kw):
        super().__init__(root=False, su_blocked=False, **kw)
        self.granted = False
        self.never_grants = never_grants
        self.wrong_foreground = wrong_foreground

    def __call__(self, args, input_text=None, timeout=900):
        self.calls.append(list(args))
        if "shell" in args:
            tail = args[-1]
            if tail.startswith("uiautomator dump"):
                return 0, "", ""
            if tail.startswith("cat /sdcard/cas_ui.xml"):
                return 0, GRANT_XML, ""
            if "topResumedActivity" in tail:
                if self.wrong_foreground:
                    return 0, "  topResumedActivity: ActivityRecord{u0 com.android.launcher3/.Launcher t1}\n", ""
                return 0, "  topResumedActivity: ActivityRecord{u0 com.topjohnwu.magisk/.SuRequestActivity}\n", ""
            if tail.startswith("input tap"):
                if not self.never_grants:
                    self.granted = True
                return 0, "", ""
            if "/debug_ramdisk/su" in args:
                cmd = args[-1]
                if cmd == "id":
                    return (0, "uid=0(root)\n", "") if self.granted else (1, "", "Permission denied")
                if cmd.startswith("sh /data/local/tmp/cas_grant.sh"):
                    return 0, "CAS_GRANT policy=2\n", ""
        # Everything else — the prompt-raise `su -c id …&` (SU is embedded in the cmd string, so it is
        # NOT a standalone arg and does not enter the su block above), `rm -f`, `boot_patch.sh`,
        # `getprop`, `wait-for-device` — falls through to FakeRunner, whose shell catch-all returns
        # (0, "", "").
        return super().__call__(args, input_text, timeout)


class GrantShellRoot(unittest.TestCase):
    def setUp(self):
        # CAS_CONFIG isolation: some tests below point it at a scratch dir and clean up with a bare
        # del/pop (assuming it was previously unset) — restore whatever it actually was (the module
        # default from setUpModule) so that assumption never has to hold true.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    def _adb(self, runner):
        return Adb("ABC123", runner=runner)

    def test_zero_touch_grant_succeeds_and_persists(self):
        r = GrantRunner()
        ok = PV.grant_shell_root(self._adb(r), log=lambda *_: None, ui_timeout=3)
        self.assertTrue(ok)
        # the permanent-policy script was pushed and run as root
        self.assertTrue(any("push" in c and PV.DEV_GRANT in c for c in r.calls))
        self.assertTrue(any("/debug_ramdisk/su" in c and c[-1].startswith("sh " + PV.DEV_GRANT)
                            for c in r.calls))

    def test_autotap_wakes_and_dismisses_keyguard(self):
        # The unit reboots to a locked/asleep screen; the auto-tap must WAKE it and slide past a
        # non-secure keyguard first — uiautomator can't see/tap the Grant dialog on an off/locked screen,
        # which is exactly why the tap silently missed and root had to be granted by hand on the bench.
        r = GrantRunner()
        PV.grant_shell_root(self._adb(r), log=lambda *_: None, ui_timeout=2)
        cmds = "\n".join(r.cmds())
        self.assertIn("input keyevent 224", cmds)               # KEYCODE_WAKEUP — screen on
        self.assertIn("wm dismiss-keyguard", cmds)              # past a swipe/none lock

    def test_await_boot_grant_confirms_zero_touch_when_marker_reports_ok(self):
        # The boot-grant service writes its marker a beat after boot_completed (it waits for magiskd);
        # _await_boot_grant polls that marker (no su/prompt) then confirms root -> true zero-touch.
        from unittest import mock

        class BootGrant(FakeRunner):
            def __init__(self, ok_after=2, **kw):
                super().__init__(root=False, **kw)
                self.reads, self.ok_after = 0, ok_after

            def __call__(self, args, input_text=None, timeout=900):
                tail = args[-1] if args else ""
                if isinstance(tail, str) and tail.startswith(f"cat {PV.BOOT_GRANT_MARK}"):
                    self.reads += 1
                    return (0, "cas-grant ok policy=2\n", "") if self.reads >= self.ok_after else (0, "", "")
                if "/debug_ramdisk/su" in args and tail == "id":
                    return (0, "uid=0\n", "") if self.reads >= self.ok_after else (1, "", "denied")
                return super().__call__(args, input_text, timeout)
        r = BootGrant(ok_after=2)
        with mock.patch("time.sleep", lambda *a, **k: None):
            self.assertTrue(PV._await_boot_grant(Adb(runner=r), log=lambda *_: None, timeout=20, step=2))

    def test_await_boot_grant_falls_back_when_daemon_not_ready(self):
        # Marker says the service ran but magiskd never came up -> don't wait the full window, fall back.
        from unittest import mock

        class NotReady(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                tail = args[-1] if args else ""
                if isinstance(tail, str) and tail.startswith(f"cat {PV.BOOT_GRANT_MARK}"):
                    return 0, "cas-grant daemon-not-ready\n", ""
                return super().__call__(args, input_text, timeout)
        with mock.patch("time.sleep", lambda *a, **k: None):
            self.assertFalse(PV._await_boot_grant(Adb(runner=NotReady(root=False)),
                                                  log=lambda *_: None, timeout=20, step=2))

    def test_await_boot_grant_short_circuits_when_already_root(self):
        # A persisted policy (re-root of an already-granted unit) -> True immediately, no marker poll.
        self.assertTrue(PV._await_boot_grant(Adb(runner=FakeRunner(root=True)), log=lambda *_: None))

    def test_failed_autotap_falls_back(self):
        logs = []
        r = GrantRunner(never_grants=True)
        ok = PV.grant_shell_root(self._adb(r), log=logs.append, attempts=2, ui_timeout=1)
        self.assertFalse(ok)
        self.assertTrue(any("open Magisk" in m for m in logs))   # manual fallback surfaced

    def test_no_tap_when_foreground_is_not_magisk(self):
        # Safety gate: if the foreground app is NOT Magisk, we must never tap 'Grant' (could hit
        # another app's button). Regression guard for the `MAGISK_PKG in foreground(...)` fence.
        r = GrantRunner(wrong_foreground=True)
        ok = PV.grant_shell_root(self._adb(r), log=lambda *_: None, attempts=1, ui_timeout=1)
        self.assertFalse(ok)
        self.assertFalse(any("input tap" in " ".join(c) for c in r.calls))

    def test_config_toggle_default_on(self):
        from cas import config
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            os.environ["CAS_CONFIG"] = os.path.join(d, "cas-config.json")  # no file -> default
            try:
                self.assertTrue(config.auto_grant_shell())
            finally:
                del os.environ["CAS_CONFIG"]

    def test_root_autogrants_when_booted_but_ungranted(self):
        import tempfile, pathlib
        from unittest import mock
        ra, fb = GrantRunner(), FbRunner()
        with tempfile.TemporaryDirectory() as d:
            stock = pathlib.Path(d) / "init_boot.img"
            stock.write_bytes(b"x")                       # PC stock image must exist
            os.environ["CAS_CONFIG"] = str(pathlib.Path(d) / "absent.json")  # missing -> default toggle on
            try:
                # GrantRunner never writes the boot-grant marker, so _await_boot_grant waits out its grace
                # window then falls back to the auto-tap — mock sleep so that grace window is instant here.
                with mock.patch("time.sleep", lambda *a, **k: None):
                    ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), stock, magisk_apk=None,
                                 log=lambda *_: None, wait=True,
                                 flasher=lambda adb, target, img, log: True)
            finally:
                os.environ.pop("CAS_CONFIG", None)
        self.assertTrue(ok)                               # root() returns True via the auto-grant tail
        self.assertTrue(ra.granted)                       # the auto-tap path actually ran

    def test_root_refuses_wrong_partition_type_image(self):
        # REGRESSION (Retroid Pocket 5 / kona brick): an init_boot image is RAMDISK-ONLY (kernel_size 0).
        # Flashed to a plain `boot` partition (a pre-init_boot unit's target) it removes the kernel and the
        # unit bootloops straight to fastboot. The inverse (a full boot.img -> init_boot) is wrong too.
        # root() must REFUSE both BEFORE flashing — model-independently (this unit sets no model_match).
        INIT_BOOT = b"ANDROID!" + (0).to_bytes(4, "little") + b"\0" * 512          # ramdisk-only, no kernel
        FULL_BOOT = b"ANDROID!" + (0x800000).to_bytes(4, "little") + b"\0" * 512   # has a kernel
        with tempfile.TemporaryDirectory() as d:
            ib = pathlib.Path(d) / "init_boot.img"; ib.write_bytes(INIT_BOOT)
            bt = pathlib.Path(d) / "boot.img";      bt.write_bytes(FULL_BOOT)
            # (a) init_boot image on a pre-init_boot unit (first_api<33 -> 'boot_a' target) -> refuse
            ra = FakeRunner(root=False, first_api="31", slot="_a")
            logs = []
            ok = PV.root(Adb(runner=ra), Fastboot(runner=FakeRunner()), ib, magisk_apk=None,
                         log=logs.append, wait=False, flasher=lambda *a: True)
            self.assertFalse(ok)
            self.assertTrue(any("REFUSING" in m for m in logs), logs)
            self.assertFalse(any("flash" in " ".join(c).lower() for c in ra.calls))   # nothing flashed
            # (b) full boot image on an A13+ unit (first_api>=33 -> 'init_boot_a' target) -> refuse
            rb = FakeRunner(root=False, first_api="33", slot="_a")
            logs2 = []
            ok2 = PV.root(Adb(runner=rb), Fastboot(runner=FakeRunner()), bt, magisk_apk=None,
                          log=logs2.append, wait=False, flasher=lambda *a: True)
            self.assertFalse(ok2)
            self.assertTrue(any("REFUSING" in m for m in logs2), logs2)

    def test_root_pre_authorized_boot_skips_autotap(self):
        # overlay.d pre-wrote the policy -> the unit boots already-root -> root() succeeds WITHOUT
        # ever invoking the uiautomator auto-tap.
        import tempfile, pathlib
        called = {"autotap": False}
        orig = PV.grant_shell_root
        PV.grant_shell_root = lambda *a, **k: called.__setitem__("autotap", True) or True
        try:
            with tempfile.TemporaryDirectory() as d:
                stock = pathlib.Path(d) / "init_boot.img"; stock.write_bytes(b"x")
                os.environ["CAS_CONFIG"] = str(pathlib.Path(d) / "absent.json")
                try:
                    ok = PV.root(Adb(runner=FakeRunner(root=True)), Fastboot(runner=FbRunner()), stock,
                                 magisk_apk=None, log=lambda *_: None, wait=True,
                                 flasher=lambda adb, target, img, log: True)
                finally:
                    del os.environ["CAS_CONFIG"]
        finally:
            PV.grant_shell_root = orig
        self.assertTrue(ok)
        self.assertFalse(called["autotap"], "auto-tap must not run when su is already pre-authorized")


def make_profile(tmp, name="odin2mini", model="Odin2 ?Mini", apps=None):
    apps = apps or ["org.es_de.frontend", "dev.eden.eden_emulator", "org.citra.emu"]
    d = pathlib.Path(tmp) / name
    pay = d / "golden_root_payload"
    pay.mkdir(parents=True)
    (d / "profile.meta").write_text(
        f"model_match={model}\nfrontend=es-de\ncaptured=2026-06-16\n"
        f"stock_init_boot=provision/root/firmware/odin2_20231201/init_boot.img\n")
    (pay / "pkglist.txt").write_text("\n".join(apps) + "\n")
    (pay / "global.meta").write_text("golden_serial=9C33-6BBD\n")
    for a in apps:                                  # a VALID payload: each app has an apk + data.tar
        (pay / a / "apk").mkdir(parents=True)
        (pay / a / "apk" / "base.apk").write_text("x")
        (pay / a / "data.tar").write_text("x")
    P.save_manifest(d / "manifest", apps, {"settings": "on", "hardening": "on"},
                    header=f"# {name}")
    return P.Profile(d)


def _seed_store(store, pkg, label, content="apk"):
    """Write a single-APK store entry directly (no put_store_apk) so read-accessor tests are self-contained."""
    d = pathlib.Path(store) / pkg
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{label}.apk").write_text(content)
    (d / "meta").write_text(f"current={label}\n")


_mk = make_profile  # alias for brevity in tests


class TestAdb(unittest.TestCase):
    def test_getprop_and_root(self):
        a = Adb(runner=FakeRunner())
        self.assertEqual(a.getprop("ro.product.model"), "Odin2 Mini")
        self.assertTrue(a.is_root())
        self.assertFalse(a.is_golden())
        self.assertTrue(a.boot_completed())

    def test_not_root(self):
        self.assertFalse(Adb(runner=FakeRunner(root=False)).is_root())

    def test_push_msg_surfaces_adb_error(self):
        # push_msg must hand back adb's error text on failure so a caller can log WHY a push died
        # (device offline / no space / read error) instead of a blind False.
        def runner(args, input_text=None, timeout=900):
            if "push" in args:
                return 1, "", "adb: error: failed to read: device offline"
            return 0, "", ""
        ok, why = Adb(runner=runner).push_msg("/src", "/dst")
        self.assertFalse(ok)
        self.assertIn("device offline", why)

    def test_push_msg_ok_is_quiet(self):
        ok, why = Adb(runner=FakeRunner(push_ok=True)).push_msg("/src", "/dst")
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_is_online(self):
        def state(val):
            return lambda args, input_text=None, timeout=900: (
                (0, val + "\n", "") if args[-1] == "get-state" else (0, "", ""))
        self.assertTrue(Adb(runner=state("device")).is_online())
        self.assertFalse(Adb(runner=state("offline")).is_online())

    def test_is_online_false_when_no_device(self):
        # no device attached -> `adb get-state` exits nonzero
        runner = lambda args, input_text=None, timeout=900: (1, "", "error: no devices/emulators found")
        self.assertFalse(Adb(runner=runner).is_online())

    def test_wait_boot_never_uses_open_ended_wait_for_device(self):
        # ROOT-CAUSE REGRESSION — Root froze at "step 4/4: waiting for the device to finish booting" on a
        # unit that HAD already booted. wait_boot() opened with a blocking `adb wait-for-device`, which
        # (a) is bounded by the RUNNER's 900s default, not wait_boot's `timeout`, (b) ran BEFORE the poll
        # loop so it emitted no on_tick progress and the cancel check never got a turn, and (c) only
        # returns once adb itself reaches the 'device' state — which stalls while the unit re-enumerates
        # after the fastbootd flash / the adb server's transport list is stale. Poll the real condition
        # instead (a fresh `adb shell getprop` re-resolves the transport). await_online() already avoids
        # wait-for-device for exactly this reason.
        class NoWaitForDevice(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                assert "wait-for-device" not in args, \
                    "wait_boot must not call the open-ended `adb wait-for-device` (blocks ~900s, no ticks)"
                return super().__call__(args, input_text, timeout)
        r = NoWaitForDevice()
        self.assertTrue(Adb(runner=r).wait_boot(timeout=10))
        self.assertNotIn("wait-for-device", "\n".join(r.cmds()))

    def test_wait_boot_recovers_when_device_is_briefly_unreachable(self):
        # The real post-flash sequence: the unit is absent from adb for a few seconds while it
        # re-enumerates, THEN answers sys.boot_completed=1. Polling must ride through the dead window and
        # detect the boot (the old wait-for-device prelude wedged here instead).
        from unittest import mock

        class SlowBoot(FakeRunner):
            def __init__(self, dead=3, **kw):
                super().__init__(**kw)
                self.dead, self.probes = dead, 0

            def __call__(self, args, input_text=None, timeout=900):
                if "shell" in args and args[-1].startswith("getprop sys.boot_completed"):
                    self.probes += 1
                    if self.probes <= self.dead:            # still re-enumerating -> adb can't reach it
                        return 1, "", "error: device '2ee078bd' not found"
                    return 0, "1\n", ""
                return super().__call__(args, input_text, timeout)
        r = SlowBoot(dead=3)
        with mock.patch("time.sleep", lambda *a, **k: None):
            self.assertTrue(Adb(runner=r).wait_boot(timeout=60))
        self.assertEqual(r.probes, 4)                        # kept polling through the dead window

    def test_wait_boot_is_bounded_and_reports_progress(self):
        # A unit that never boots must FAIL inside its own `timeout` (not the runner's 900s) and must emit
        # progress ticks so the operator sees it working rather than a frozen UI.
        from unittest import mock
        ticks = []
        r = FakeRunner(boot="0")                             # sys.boot_completed never becomes 1
        with mock.patch("time.sleep", lambda *a, **k: None):
            # on_tick now also receives the adb device state (2nd arg) so the caller can say WHY.
            self.assertFalse(Adb(runner=r).wait_boot(timeout=60, on_tick=lambda s, st=None: ticks.append(s)))
        self.assertEqual(ticks, [10, 20, 30, 40, 50])        # ~every 10s, bounded by timeout=60

    def test_state_reports_the_adb_connection_state(self):
        # wait_boot leans on state() to turn a stalled boot-wait into an actionable reason. Modern adb
        # prints the state to stdout; older adb errors on stderr — both must classify. Absent -> ''.
        def st(rc, out, err=""):
            class S(FakeRunner):
                def __call__(self, args, input_text=None, timeout=900):
                    if args and args[-1] == "get-state":
                        return rc, out, err
                    return super().__call__(args, input_text, timeout)
            return Adb(runner=S()).state()
        self.assertEqual(st(0, "device\n"), "device")
        self.assertEqual(st(0, "unauthorized\n"), "unauthorized")
        self.assertEqual(st(1, "", "error: device unauthorized"), "unauthorized")   # older adb
        self.assertEqual(st(1, "", "error: device 'X' not found"), "")              # absent

    def test_boot_tick_msg_is_actionable_per_state(self):
        # A unit that re-appears 'unauthorized' after the root-flash reboot is sitting on a locked
        # 'Allow USB debugging' prompt — the tick line must say to unlock + accept it, not hide it
        # behind a bland 'still booting'.
        from cas.adb import boot_tick_msg
        un = boot_tick_msg(40, "unauthorized")
        self.assertIn("40s", un)
        self.assertIn("Allow USB debugging", un)
        self.assertIn("unlock", un.lower())
        # reachable but not yet booted: still nudge an unlock (FBE gates boot_completed until first unlock)
        self.assertIn("unlock", boot_tick_msg(40, "device").lower())
        # offline / absent: waiting to re-appear on USB
        self.assertIn("re-appear", boot_tick_msg(40, "").lower())

    def test_wait_boot_surfaces_unauthorized_instead_of_bland_still_booting(self):
        # THE BENCH BUG (Retroid root): after the root-flash reboot the unit comes back 'unauthorized'
        # (its 'Allow USB debugging' prompt waits on a LOCKED screen). The old wait_boot polled only
        # sys.boot_completed and hid every failure behind 'still booting', so the operator never knew to
        # unlock + accept — the wait just ran to timeout. wait_boot must feed the adb state to on_tick.
        from unittest import mock
        from cas.adb import boot_tick_msg

        class Unauth(FakeRunner):
            def __init__(self, **kw):
                super().__init__(boot="0", **kw)                 # boot_completed never returns 1
            def __call__(self, args, input_text=None, timeout=900):
                if args and args[-1] == "get-state":
                    return 0, "unauthorized\n", ""               # re-appeared, but not authorized
                return super().__call__(args, input_text, timeout)
        msgs = []
        with mock.patch("time.sleep", lambda *a, **k: None):
            ok = Adb(runner=Unauth()).wait_boot(
                timeout=60, on_tick=lambda s, st: msgs.append(boot_tick_msg(s, st)))
        self.assertFalse(ok)
        self.assertTrue(any("Allow USB debugging" in m for m in msgs),
                        f"expected an actionable unauthorized hint, got: {msgs}")

    def test_boot_flash_target_init_boot_ab(self):
        # A unit launched on Android 13+ (first_api>=33) is A/B -> flash 'init_boot_<active slot>'.
        self.assertEqual(Adb(runner=FakeRunner(first_api="33", slot="_a")).boot_flash_target(), "init_boot_a")
        self.assertEqual(Adb(runner=FakeRunner(first_api="33", slot="_b")).boot_flash_target(), "init_boot_b")

    def test_boot_flash_target_legacy_boot(self):
        # A unit that did NOT launch on 13 (first_api<33) keeps the patchable ramdisk in 'boot', not init_boot.
        self.assertEqual(Adb(runner=FakeRunner(first_api="31", slot="_a")).boot_flash_target(), "boot_a")

    def test_boot_flash_target_a_only(self):
        # A-only device (empty slot_suffix) -> no slot suffix appended.
        self.assertEqual(Adb(runner=FakeRunner(first_api="33", slot="")).boot_flash_target(), "init_boot")

    def test_list_devices(self):
        devs = list_devices(runner=FakeRunner())
        self.assertEqual(devs, [("ABC123", "device"), ("DEF456", "unauthorized")])

    def test_serial_scoping(self):
        r = FakeRunner()
        Adb(serial="XYZ", runner=r).getprop("ro.product.model")
        self.assertIn("-s", r.calls[0])
        self.assertIn("XYZ", r.calls[0])

    def test_raw_honors_cancel_event(self):
        """Cancel must abort plain adb ops too (push/shell/raw) — not just the *_stream calls — so a
        Download (whose big phase is adb push, via raw) actually stops when the operator hits Cancel."""
        import threading, time
        from cas.adb import Adb, is_cancelled
        ev = threading.Event()
        a = Adb(adb="sleep", cancel=ev)                 # real subprocess_runner; `adb` stands in as `sleep`
        out = {}
        th = threading.Thread(target=lambda: out.__setitem__("r", a.raw("10")))   # `sleep 10`
        th.start()
        time.sleep(0.4); ev.set()                        # operator cancels shortly after it starts
        th.join(timeout=6)
        self.assertFalse(th.is_alive(), "raw() ignored cancel — the child kept running")
        self.assertTrue(is_cancelled(out["r"][0]), f"expected CANCELLED rc, got {out['r'][0]}")

    def test_sd_info(self):
        self.assertIn("9C33-6BBD", Adb(runner=FakeRunner(sd=True)).sd_info())
        self.assertEqual(Adb(runner=FakeRunner(sd=False)).sd_info(), "no SD")

    def test_sd_detected_regardless_of_volume_id_format(self):
        # A big exFAT card mounts as a hyphen-LESS 16-hex volume id (e.g. Retroid Pocket 6's ROM card at
        # /storage/6ED25E36D25E032F). Detection must NOT assume the FAT 'XXXX-XXXX' shape, or the card —
        # and thus the auto-matched profile/tier — silently reads as 'no SD'.
        for vol in ("6ED25E36D25E032F", "9C33-6BBD", "ABCD-1234"):
            r = FakeRunner(sd_vols=[vol])
            self.assertIn(vol, Adb(runner=r).sd_info(), f"{vol} not detected")
            self.assertTrue(Adb(runner=r).has_sd(), f"has_sd False for {vol}")
        # only the internal 'emulated'/'self' present -> genuinely no card
        self.assertEqual(Adb(runner=FakeRunner(sd_vols=[])).sd_info(), "no SD")
        self.assertFalse(Adb(runner=FakeRunner(sd_vols=[])).has_sd())

    def test_pull_with_progress_fallback(self):
        # an injected (test) runner has no real process to poll -> one blocking pull; success follows pull_ok
        self.assertTrue(Adb(runner=FakeRunner(pull_ok=True))
                        .pull_with_progress("/d/src", "/d/dst", 100, lambda m: None))
        self.assertFalse(Adb(runner=FakeRunner(pull_ok=False))
                         .pull_with_progress("/d/src", "/d/dst", 100, lambda m: None))

    def test_dir_size_kb_missing_dir(self):
        from cas import adb as A
        # pull hasn't created the dir yet -> 0, not a crash (best-effort sizing for the progress bar)
        self.assertEqual(A._dir_size_kb("/no/such/path/cas-test"), 0)

    def test_subprocess_runner_suppresses_console_window(self):
        # cas-gui.exe is a GUI app; adb/fastboot calls must pass creationflags so Windows
        # doesn't flash a console window per call (0 off-Windows; CREATE_NO_WINDOW on it).
        from cas import adb as A
        from unittest import mock
        seen = {}

        class _R:
            returncode, stdout, stderr = 0, "ok", ""

        def fake_run(args, **kw):
            seen.update(kw)
            return _R()

        with mock.patch.object(A.subprocess, "run", fake_run):
            rc, out, _ = A.subprocess_runner(["adb", "devices"])
        self.assertEqual((rc, out), (0, "ok"))
        self.assertIn("creationflags", seen)


class TestFastboot(unittest.TestCase):
    def test_remaps_to_present_device_when_fastboot_serial_differs(self):
        # MANGMI reports a DIFFERENT serial in fastboot (357451cb) than adb (MQ66…). Without remap,
        # `fastboot -s MQ66… flash` hangs forever; the flash must target the serial actually present.
        calls = []

        def runner(args, input_text=None, timeout=900):
            calls.append(list(args))
            if args[-1] == "devices":
                return 0, "357451cb\t fastboot\n", ""
            return 0, "", ""
        fb = Fastboot(serial="MQ66142509130541", runner=runner)
        self.assertTrue(fb.wait(timeout=2))
        self.assertEqual(fb.resolve(), "357451cb")
        self.assertTrue(fb.flash("init_boot_b", "/tmp/x.img"))
        flash_cmd = [c for c in calls if "flash" in c][-1]
        self.assertIn("357451cb", flash_cmd)                 # targets the real fastboot serial
        self.assertNotIn("MQ66142509130541", flash_cmd)      # NOT the adb serial (would hang)

    def test_keeps_requested_serial_when_present(self):
        def runner(args, input_text=None, timeout=900):
            if args[-1] == "devices":
                return 0, "SAME123\t fastboot\n", ""
            return 0, "", ""
        fb = Fastboot(serial="SAME123", runner=runner)
        self.assertTrue(fb.wait(timeout=2))
        self.assertEqual(fb.resolve(), "SAME123")            # Retroid/AYN: serial matches, unchanged

    def test_ambiguous_keeps_requested_serial(self):
        # several units in fastboot, requested serial absent -> don't guess; keep requested (fails loudly).
        def runner(args, input_text=None, timeout=900):
            if args[-1] == "devices":
                return 0, "aaa\t fastboot\nbbb\t fastboot\n", ""
            return 0, "", ""
        fb = Fastboot(serial="MQ66", runner=runner)
        self.assertEqual(fb.resolve(), "MQ66")


class TestManifestNewlines(unittest.TestCase):
    """ROOT-CAUSE REGRESSION — Windows Download: "no APK in payload" for EVERY app while the payload sat on
    the device, complete. save_manifest used pathlib.write_text(), whose text mode translates "\\n" ->
    os.linesep, so on Windows the manifest pushed to the device carried CRLF. restore.sh/capture.sh parse it
    with awk, whose default field separator does NOT include \\r, so every bare package line yielded
    "$pkg" = "com.foo\\r" and "$P/$pkg/apk/" named a path that cannot exist. (Python's str.split() strips \\r,
    so the PC-side _validate_payload passed on the SAME file — the two sides disagreed, which is what made
    this look like a transfer bug.) The manifest is a DEVICE-consumed file: it must be LF-only on every OS.
    This assertion is what the windows-latest CI runner enforces (on POSIX write_text already emits LF)."""

    def test_save_manifest_is_lf_only_bytes(self):
        with tempfile.TemporaryDirectory() as t:
            m = pathlib.Path(t) / "manifest"
            P.save_manifest(m, ["com.foo", "com.bar"], {"settings": "on"},
                            header="# p (deploy)", axes={"com.bar": (True, False)})
            raw = m.read_bytes()
            self.assertNotIn(b"\r", raw, "device-consumed manifest must never carry CR (breaks awk on-device)")
            self.assertIn(b"\ncom.foo\n", raw)          # bare line = the shape that carried the stray \r
            self.assertTrue(raw.endswith(b"\n"))

    def test_capture_and_deploy_manifests_share_the_lf_writer(self):
        # Both device-pushed manifests (Download's {DEV}/manifest and Save's capture-manifest) go through
        # save_manifest, so the LF fix must cover both. Guard that neither can regress to text mode.
        with tempfile.TemporaryDirectory() as t:
            for name in ("manifest", "capture-manifest"):
                p = pathlib.Path(t) / name
                P.save_manifest(p, ["com.foo"], {"grants": "off"}, header=f"# {name}")
                self.assertNotIn(b"\r", p.read_bytes(), f"{name} must be LF-only")


class TestManifestAxes(unittest.TestCase):
    def _write(self, text):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "manifest").write_text(text)
        return d / "manifest"

    def test_bare_line_is_both_axes(self):
        m = self._write("# h\ncom.foo\n")
        self.assertEqual(P.manifest_axes(m), {"com.foo": (True, True)})

    def test_apk_only_and_config_only(self):
        m = self._write("com.bar apk\nxyz.aethersx2.android config\n")
        ax = P.manifest_axes(m)
        self.assertEqual(ax["com.bar"], (True, False))
        self.assertEqual(ax["xyz.aethersx2.android"], (False, True))

    def test_both_tokens_order_insensitive_and_flags_ignored(self):
        m = self._write("com.baz config apk\n@settings on\n")
        self.assertEqual(P.manifest_axes(m), {"com.baz": (True, True)})


class TestProfiles(unittest.TestCase):
    def test_manifest_parse(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            self.assertEqual(prof.pkgs(),
                             ["org.es_de.frontend", "dev.eden.eden_emulator", "org.citra.emu"])
            self.assertEqual(prof.flags(), {"settings": "on", "hardening": "on"})

    def test_manifest_tolerates_non_utf8_bytes(self):
        # NAS profiles authored on Windows can carry cp1252 bytes (e.g. em-dash 0x97). Strict UTF-8 used to
        # crash the whole GUI on startup; the readers must decode tolerantly and still find ASCII pkgs/flags.
        with tempfile.TemporaryDirectory() as t:
            m = pathlib.Path(t) / "manifest"
            m.write_bytes(b"# Retroid \x97 ESDE build\norg.es_de.frontend\n@settings on\n")
            self.assertEqual(P.manifest_pkgs(m), ["org.es_de.frontend"])
            self.assertEqual(P.manifest_flags(m), {"settings": "on"})
            meta = pathlib.Path(t) / "profile.meta"
            meta.write_bytes(b"frontend=es\x97de\nmodel_match=Foo\n")
            self.assertEqual(P._read_meta(meta)["model_match"], "Foo")   # no crash; other keys parse

    def test_set_meta_key_updates_existing_and_adds_new(self):
        # The GUI's 'Root images' Browse writes stock_init_boot / magisk_apk into profile.meta in place,
        # leaving other keys + comments intact.
        with tempfile.TemporaryDirectory() as t:
            m = pathlib.Path(t) / "profile.meta"
            m.write_text("# header comment\nmodel_match=Foo\nfrontend=es-de\n")
            P.set_meta_key(m, "frontend", "retroarch")                                  # update in place
            P.set_meta_key(m, "stock_init_boot", "provision/root/firmware/x/init_boot.img")  # append new
            meta = P._read_meta(m)
            self.assertEqual(meta["frontend"], "retroarch")
            self.assertEqual(meta["model_match"], "Foo")                                # untouched
            self.assertEqual(meta["stock_init_boot"], "provision/root/firmware/x/init_boot.img")
            self.assertIn("# header comment", m.read_text())                            # comments preserved

    def test_resolve_asset_prefers_profile_local_then_appdir(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, "p", "M")
            (prof.path / "patched_init_boot.img").write_bytes(b"x")         # captured: lives in the profile
            # profile-local file wins even though appdir has no such file
            self.assertEqual(P.resolve_asset(prof, t, "patched_init_boot.img"),
                             prof.path / "patched_init_boot.img")
            # a name that only exists appdir-relative (the shared firmware library) falls back to appdir
            (pathlib.Path(t) / "shared.img").write_bytes(b"x")
            self.assertEqual(P.resolve_asset(prof, t, "shared.img"), pathlib.Path(t) / "shared.img")

    def test_match_profile(self):
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "odin2mini", "Odin2 ?Mini")
            make_profile(t, "mangmi-airx-256", "Air ?X|Mangmi")
            self.assertEqual(P.match_profile("Odin2 Mini", t).name, "odin2mini")
            self.assertEqual(P.match_profile("Mangmi Air X", t).name, "mangmi-airx-256")
            self.assertIsNone(P.match_profile("Retroid Pocket 6", t))

    def test_match_profile_ambiguous_and_empty(self):
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "a", "Odin2 ?Mini")
            make_profile(t, "b", "Odin2 Mini")          # both end-match "Odin2 Mini"
            self.assertIsNone(P.match_profile("Odin2 Mini", t))   # ambiguous -> refuse to guess
            self.assertIsNone(P.match_profile("", t))             # blank model -> None
            self.assertIsNone(P.match_profile(None, t))

    def test_parse_sd_gb(self):
        self.assertEqual(P.parse_sd_gb("9C33-6BBD · 477G"), 477.0)
        self.assertEqual(P.parse_sd_gb("238G"), 238.0)
        self.assertEqual(P.parse_sd_gb("1T"), 1024.0)
        self.assertIsNone(P.parse_sd_gb("no SD"))
        self.assertIsNone(P.parse_sd_gb(""))

    def test_match_by_name_similarity_no_regex(self):
        # model_match BLANK -> matched purely by NAME similarity (no regex to write)
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "retroid-pocket-6-512", model="")
            self.assertEqual(P.match_profile("Retroid Pocket 6", t).name, "retroid-pocket-6-512")
            self.assertIsNone(P.match_profile("Odin2 Mini", t))          # different model -> no match

    def test_match_tier_by_sd_size(self):
        # two capacity tiers, no regex -> the device's SD size chooses the closest one
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "retroid-pocket-6-512", model="")
            make_profile(t, "retroid-pocket-6-256", model="")
            self.assertEqual(P.match_profile("Retroid Pocket 6", t, sd_gb=477).name, "retroid-pocket-6-512")
            self.assertEqual(P.match_profile("Retroid Pocket 6", t, sd_gb=238).name, "retroid-pocket-6-256")
            self.assertIsNone(P.match_profile("Retroid Pocket 6", t))    # no size -> ambiguous, assign manually

    def test_model_matches_regex_backcompat(self):
        # hand-written regex patterns keep working exactly as before
        self.assertTrue(P.model_matches("Odin2 ?Mini", "Odin2 Mini"))
        self.assertTrue(P.model_matches("Odin2.*Mini", "Odin2 Retro Mini"))
        self.assertTrue(P.model_matches("Retroid Pocket 6", "Retroid Pocket 6"))
        self.assertFalse(P.model_matches("Odin2 ?Mini", "Retroid Pocket 6"))

    def test_model_matches_tolerates_capacity_in_pattern(self):
        # the operator typed the STORAGE TIER into the model — ro.product.model never carries capacity,
        # so the raw regex can't match. The tier is not model identity: still the same unit.
        self.assertTrue(P.model_matches("Retroid Pocket 6 256", "Retroid Pocket 6"))
        self.assertTrue(P.model_matches("Retroid Pocket 6 512", "Retroid Pocket 6"))
        self.assertTrue(P.model_matches("Ayn Odin 3 256", "Ayn Odin 3"))
        self.assertTrue(P.model_matches("Mangmi Air X 256", "MANGMI AIR X"))     # case-insensitive tokens

    def test_model_matches_still_refuses_wrong_model(self):
        # THE BRICK GUARD: a model VERSION is not a capacity (< _CAP_MIN_GB), so it stays significant.
        # An RP5 profile must never fit an RP6 — that wrong-image flash is what bricked the RP5.
        self.assertFalse(P.model_matches("Retroid Pocket 5", "Retroid Pocket 6"))
        self.assertFalse(P.model_matches("Retroid Pocket 5 256", "Retroid Pocket 6"))
        self.assertFalse(P.model_matches("Ayn Odin 2 256", "Ayn Odin 3"))
        self.assertFalse(P.model_matches("Odin2 Mini", "Retroid Pocket 6"))

    def test_model_matches_edges(self):
        self.assertFalse(P.model_matches("Retroid Pocket 6", ""))       # model unknown -> refuse (safe)
        self.assertFalse(P.model_matches("", "Retroid Pocket 6"))       # no pattern -> caller skips guard
        self.assertFalse(P.model_matches("256", "Retroid Pocket 6"))    # capacity ONLY -> no model words left
        self.assertFalse(P.model_matches("Retroid Pocket 6[", ""))      # invalid regex must not raise

    def test_golden_presence_and_size(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)                              # writes global.meta + per-app data.tar
            self.assertTrue(prof.has_golden())
            self.assertGreater(prof.golden_size(), 0)
            d = pathlib.Path(t) / "empty"
            d.mkdir()
            (d / "profile.meta").write_text("model_match=\n")
            (d / "manifest").write_text("# empty\n")
            empty = P.Profile(d)
            self.assertFalse(empty.has_golden())               # no payload -> no golden
            self.assertEqual(empty.golden_size(), 0)

    def test_internal_for(self):
        self.assertEqual(P.internal_for("org.es_de.frontend"), "ES-DE")
        self.assertEqual(P.internal_for("com.retroarch.aarch64"), "RetroArch")
        self.assertIsNone(P.internal_for("dev.eden.eden_emulator"))

    def test_archive_is_soft_delete(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, "doomed")
            dst = P.archive_profile(prof, "20260616")
            self.assertFalse((pathlib.Path(t) / "doomed").exists())   # moved, not present
            self.assertTrue(dst.exists())                              # recoverable in _archive
            self.assertIn("_archive", str(dst))

    def test_capture_manifest_accessors_and_emulator_set(self):
        import tempfile, pathlib
        from cas import profiles as P
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "profile.meta").write_text("frontend=es-de\n")
        (d / "capture-manifest").write_text("# cap\ncom.foo\nbar.app config\n@gamelauncher on\n")
        prof = P.Profile(d)
        self.assertEqual(prof.capture_pkgs(), ["com.foo", "bar.app"])
        self.assertEqual(prof.capture_axes(), {"com.foo": (True, True), "bar.app": (False, True)})
        self.assertEqual(prof.capture_flags().get("gamelauncher"), "on")
        self.assertIn("com.retroarch.aarch64", P.EMULATOR_PKGS)        # the known emulator set exists

    def test_default_capture_checks_emulators_and_game_launcher(self):
        from cas import profiles as P
        device_apps = ["com.retroarch.aarch64", "org.ppsspp.ppsspp", "com.random.note", "com.foo.bar"]
        checked = P.default_capture_selection(device_apps, game_launcher="com.handheld.launcher")
        # emulators + game launcher on; unrelated apps off
        self.assertEqual(checked["com.retroarch.aarch64"], (True, True))
        self.assertEqual(checked["org.ppsspp.ppsspp"], (True, True))
        self.assertEqual(checked["com.random.note"], (False, False))
        self.assertEqual(checked["com.handheld.launcher"], (False, True))   # launcher = config-only

    def test_default_capture_always_install_forces_apk_on(self):
        from cas import profiles as P
        apps = ["com.valvesoftware.steamlink", "com.github.stenzek.duckstation", "com.random.app"]
        ai = frozenset({"com.valvesoftware.steamlink"})
        sel = P.default_capture_selection(apps, always_install=ai)
        self.assertEqual(sel["com.valvesoftware.steamlink"], (True, False))   # non-emulator member: APK on, Config policy-off
        self.assertEqual(sel["com.github.stenzek.duckstation"], (True, True)) # emulator unchanged
        self.assertEqual(sel["com.random.app"], (False, False))              # non-member unchanged
        # a member that is ALSO config-only (sideloaded) still gets APK on (always-install wins)
        sel2 = P.default_capture_selection(["xyz.aethersx2.tturnip"],
                                           always_install=frozenset({"xyz.aethersx2.tturnip"}))
        self.assertEqual(sel2["xyz.aethersx2.tturnip"], (True, True))
        # back-compat: no always_install arg == today's behavior
        self.assertEqual(P.default_capture_selection(["com.random.app"]), {"com.random.app": (False, False)})

    def test_initial_capture_always_install_overrides_stale_manifest(self):
        from cas import profiles as P
        apps = ["com.valvesoftware.steamlink", "com.random.app"]
        saved = {"com.valvesoftware.steamlink": (False, False),   # stale: APK previously unticked
                 "com.random.app": (True, True)}
        ai = frozenset({"com.valvesoftware.steamlink"})
        sel = P.initial_capture_selection(apps, saved, {}, always_install=ai)
        self.assertEqual(sel["com.valvesoftware.steamlink"], (True, False))  # APK re-asserted on; Config from saved (False)
        self.assertEqual(sel["com.random.app"], (True, True))               # non-member honors saved manifest
        # back-compat: no always_install arg == today's behavior (saved manifest wins)
        sel2 = P.initial_capture_selection(apps, saved, {})
        self.assertEqual(sel2["com.valvesoftware.steamlink"], (False, False))

    def test_toggle_always_member(self):
        from cas import profiles as P
        # absent -> added; present -> removed; other members preserved
        self.assertEqual(P.toggle_always_member({"a", "b"}, "c"), frozenset({"a", "b", "c"}))
        self.assertEqual(P.toggle_always_member({"a", "b"}, "a"), frozenset({"b"}))
        # toggling the last member off -> empty (which set_always_install_pkgs treats as "disable")
        self.assertEqual(P.toggle_always_member({"a"}, "a"), frozenset())
        # None/empty current -> just the added pkg
        self.assertEqual(P.toggle_always_member(None, "x"), frozenset({"x"}))

    def test_store_read_accessors(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.cocoon.app", "cocoon-1.5.0", content="bytes")
            self.assertEqual(P.store_current_label(store, "org.cocoon.app"), "cocoon-1.5.0")
            files = P.store_apk_files(store, "org.cocoon.app")
            self.assertEqual([f.name for f in files], ["cocoon-1.5.0.apk"])
            self.assertEqual(P.list_store_apks(store),
                             [{"pkg": "org.cocoon.app", "label": "cocoon-1.5.0",
                               "nfiles": 1, "bytes": len("bytes")}])

    def test_store_split_label_returns_all_apks(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"; d = store / "com.split" / "v2"; d.mkdir(parents=True)
            (d / "base.apk").write_text("a"); (d / "split_config.apk").write_text("b")
            (store / "com.split" / "meta").write_text("current=v2\n")
            self.assertEqual(sorted(f.name for f in P.store_apk_files(store, "com.split")),
                             ["base.apk", "split_config.apk"])

    def test_store_empty_and_missing(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            self.assertEqual(P.list_store_apks(store), [])                 # missing dir -> []
            self.assertIsNone(P.store_current_label(store, "nope"))
            self.assertEqual(P.store_apk_files(store, "nope"), [])

    def test_list_store_apks_sorted_by_pkg(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.zeta", "v1")
            _seed_store(store, "com.alpha", "v1")
            self.assertEqual([a["pkg"] for a in P.list_store_apks(store)], ["com.alpha", "org.zeta"])

    def test_put_defaults_label_and_sets_current(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            src = pathlib.Path(t) / "cocoon-1.5.0.apk"; src.write_text("v15")
            label = P.put_store_apk(store, "org.cocoon.app", src)
            self.assertEqual(label, "cocoon-1.5.0")                          # default label = filename stem
            self.assertEqual(P.store_current_label(store, "org.cocoon.app"), "cocoon-1.5.0")
            self.assertEqual((store / "org.cocoon.app" / "cocoon-1.5.0.apk").read_text(), "v15")

    def test_second_put_repoints_current_and_retains_prior(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            a = pathlib.Path(t) / "cocoon-1.4.0.apk"; a.write_text("v14")
            b = pathlib.Path(t) / "cocoon-1.5.0.apk"; b.write_text("v15")
            P.put_store_apk(store, "org.cocoon.app", a)
            P.put_store_apk(store, "org.cocoon.app", b)
            self.assertEqual(P.store_current_label(store, "org.cocoon.app"), "cocoon-1.5.0")
            self.assertTrue((store / "org.cocoon.app" / "cocoon-1.4.0.apk").is_file())   # prior label kept

    def test_reused_label_archives_prior_bytes(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            old = pathlib.Path(t) / "old.apk"; old.write_text("old")
            new = pathlib.Path(t) / "new.apk"; new.write_text("new")
            P.put_store_apk(store, "p", old, label="v1")
            P.put_store_apk(store, "p", new, label="v1")                     # re-use label
            self.assertEqual((store / "p" / "v1.apk").read_text(), "new")
            arch = list((store / "p" / "_archive").glob("v1.apk*"))
            self.assertEqual([a.read_text() for a in arch], ["old"])

    def test_remove_clears_current_but_keeps_files(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            src = pathlib.Path(t) / "cocoon-1.5.0.apk"; src.write_text("v15")
            P.put_store_apk(store, "org.cocoon.app", src)
            P.remove_store_apk(store, "org.cocoon.app")
            self.assertIsNone(P.store_current_label(store, "org.cocoon.app"))
            self.assertTrue((store / "org.cocoon.app" / "cocoon-1.5.0.apk").is_file())   # bytes retained
            self.assertEqual(P.list_store_apks(store), [])                               # not listed

    def test_resolve_prefers_payload_then_store_then_bundle(self):
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "p", apps=["com.captured"])                  # captured app has a payload apk
            store = pathlib.Path(t) / "store"
            self.assertEqual([f.name for f in P.resolve_app_apk("com.captured", prof, store)], ["base.apk"])
            _seed_store(store, "org.cocoon.app", "v1")
            self.assertEqual([f.name for f in P.resolve_app_apk("org.cocoon.app", prof, store)], ["v1.apk"])
            b = pathlib.Path(t) / "kit.apk"; b.write_text("x")
            self.assertEqual(P.resolve_app_apk("com.kit", prof, store, bundle_fallback=b), [b])
            self.assertIsNone(P.resolve_app_apk("com.absent", prof, store))

    def test_resolve_split_store_returns_list(self):
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"; d = store / "com.split" / "v2"; d.mkdir(parents=True)
            (d / "base.apk").write_text("a"); (d / "split_config.apk").write_text("b")
            (store / "com.split" / "meta").write_text("current=v2\n")
            files = P.resolve_app_apk("com.split", None, store)
            self.assertEqual(sorted(f.name for f in files), ["base.apk", "split_config.apk"])

    def test_download_rows_golden_drives_defaults(self):
        # 'a' captured WITH apk+config, 'b' apk-only (apk, no config), 'store1' store-only (managed).
        rows, cfg_disabled = P.download_rows(["a", "b"], ["b", "store1"],
                                             has_apk={"a": True, "b": True},
                                             has_config={"a": True, "b": False})
        self.assertEqual(rows, {"a": (True, True),         # captured apk + config -> APK on, Config on
                                "b": (True, False),        # captured apk-only -> APK on, Config off
                                "store1": (False, False)}) # store-only -> APK OFF (opt-in), no config
        # Config box is disabled wherever the golden captured nothing to restore.
        self.assertEqual(cfg_disabled, {"b", "store1"})

    def test_download_rows_always_install_auto_ticks_apk(self):
        from cas import profiles as P
        own = ["com.github.stenzek.duckstation", "com.cfgonly"]
        store = ["com.valvesoftware.steamlink"]     # store-only, not in the golden
        has_apk = {"com.github.stenzek.duckstation": True, "com.cfgonly": False}  # cfgonly: config-only capture, no bundled apk
        has_cfg = {"com.github.stenzek.duckstation": True, "com.cfgonly": True}
        ai = frozenset({"com.valvesoftware.steamlink", "com.cfgonly"})
        rows, disabled = P.download_rows(own, store, has_apk, has_cfg, always_install=ai)
        self.assertEqual(rows["com.valvesoftware.steamlink"], (True, False))     # store member auto-ticks APK
        self.assertIn("com.valvesoftware.steamlink", disabled)                   # no captured config -> disabled
        self.assertEqual(rows["com.cfgonly"], (True, True))                      # golden member, has_apk False -> APK forced on
        self.assertEqual(rows["com.github.stenzek.duckstation"], (True, True))   # non-member unchanged
        # regression: a store-only NON-member stays OFF
        rows2, _ = P.download_rows([], ["com.other"], {}, {}, always_install=ai)
        self.assertEqual(rows2["com.other"], (False, False))

    def test_download_rows_apk_default_follows_captured_apk(self):
        # 'a' has a captured apk; 'b' is config-only (config captured, NO apk — e.g. a sideloaded emulator).
        rows, cfg_disabled = P.download_rows(["a", "b"], [],
                                             has_apk={"a": True, "b": False},
                                             has_config={"a": True, "b": True})
        self.assertEqual(rows["a"], (True, True))
        self.assertEqual(rows["b"], (False, True))   # no captured apk -> APK off (you sideload it); Config on
        self.assertEqual(cfg_disabled, set())        # both have captured config

    def test_default_capture_config_only_for_sideloaded_pkg(self):
        # Both PS2 builds (.android = AetherSX2, .tturnip = NetherSX2-Turnip) are sideloaded -> config only.
        sel = P.default_capture_selection(["xyz.aethersx2.android", "xyz.aethersx2.tturnip",
                                           "com.github.stenzek.duckstation"])
        self.assertEqual(sel["xyz.aethersx2.android"], (False, True))      # config-only
        self.assertEqual(sel["xyz.aethersx2.tturnip"], (False, True))      # config-only (now recognised)
        self.assertEqual(sel["com.github.stenzek.duckstation"], (True, True))  # normal emulator: both axes

    def test_initial_capture_excludes_apps_not_on_device(self):
        # A saved manifest (from another unit) lists AetherSX2, but THIS device only has NetherSX2. The Save
        # list must reflect what's actually installed — never show a saved app that isn't on the scanned unit.
        sel = P.initial_capture_selection(
            ["xyz.aethersx2.tturnip"],                                   # device has only NetherSX2
            saved_axes={"xyz.aethersx2.android": (True, True),          # manifest lists AetherSX2 (NOT installed)
                        "xyz.aethersx2.tturnip": (False, True)},
            saved_flags={})
        self.assertIn("xyz.aethersx2.tturnip", sel)                     # installed -> shown
        self.assertNotIn("xyz.aethersx2.android", sel)                  # not installed -> NOT shown

    def test_initial_capture_forces_apk_off_for_config_only(self):
        # A stale saved manifest with the PS2 app as APK+Config must NOT re-enable APK capture — the
        # sideloaded-build policy (config only) wins so the operator never bundles the PS2 APK by accident.
        sel = P.initial_capture_selection(
            ["xyz.aethersx2.android"],
            saved_axes={"xyz.aethersx2.android": (True, True)},   # old manifest had both axes
            saved_flags={})
        self.assertEqual(sel["xyz.aethersx2.android"], (False, True))   # APK forced off; config kept

    def test_has_captured_apk_reflects_payload_apk(self):
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "p", apps=["com.withapk"])           # _mk seeds apk/base.apk
            pay = prof.payload
            (pay / "com.cfgonly").mkdir(parents=True)          # config captured, NO apk dir
            (pay / "com.cfgonly" / "data.tar").write_text("x")
            self.assertTrue(prof.has_captured_apk("com.withapk"))
            self.assertFalse(prof.has_captured_apk("com.cfgonly"))
            self.assertFalse(prof.has_captured_apk("org.store.only"))

    def test_has_captured_config_reflects_captured_data(self):
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "p", apps=["com.withcfg"])       # _mk seeds apk + data.tar
            pay = prof.payload
            (pay / "com.apkonly" / "apk").mkdir(parents=True)   # apk captured, NO data.tar
            (pay / "com.apkonly" / "apk" / "base.apk").write_text("x")
            (pay / "com.adataonly").mkdir(parents=True)         # only external data captured
            (pay / "com.adataonly" / "adata.tar").write_text("x")
            self.assertTrue(prof.has_captured_config("com.withcfg"))
            self.assertFalse(prof.has_captured_config("com.apkonly"))
            self.assertTrue(prof.has_captured_config("com.adataonly"))   # adata.tar counts as config
            self.assertFalse(prof.has_captured_config("org.store.only"))  # no payload module at all


class TestApkPackageId(unittest.TestCase):
    """profiles.apk_package_id reads the <manifest package='…'> id straight from an APK's binary
    AndroidManifest.xml (pure-Python AXML parse, no aapt) — so Add-APK can auto-fill the package id."""

    @staticmethod
    def _axml(pkg):
        """A minimal but spec-valid binary AndroidManifest.xml declaring `package=pkg` (UTF-16 pool)."""
        import struct
        strs = ["manifest", "package", pkg]
        offsets = b""; body = b""
        for s in strs:
            offsets += struct.pack("<I", len(body))
            body += struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\x00\x00"
        while len(body) % 4:
            body += b"\x00"
        strings_start = 28 + len(offsets)
        pool = struct.pack("<HHIIIIII", 0x0001, 28, strings_start + len(body), len(strs), 0, 0,
                           strings_start, 0) + offsets + body
        attr = struct.pack("<IIIHBBI", 0xFFFFFFFF, 1, 2, 8, 0, 0x03, 2)
        ext = struct.pack("<IIHHHHHH", 0xFFFFFFFF, 0, 0x14, 0x14, 1, 0, 0, 0)
        elem_body = struct.pack("<II", 1, 0xFFFFFFFF) + ext + attr
        start = struct.pack("<HHI", 0x0102, 0x10, 8 + len(elem_body)) + elem_body
        return struct.pack("<HHI", 0x0003, 8, 8 + len(pool) + len(start)) + pool + start

    def _apk(self, tmp, pkg):
        import zipfile
        p = pathlib.Path(tmp) / "x.apk"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("AndroidManifest.xml", self._axml(pkg))
        return p

    def test_reads_package_from_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(P.apk_package_id(self._apk(t, "com.example.app")), "com.example.app")
            self.assertEqual(P.apk_package_id(self._apk(t, "xyz.aethersx2.tturnip")), "xyz.aethersx2.tturnip")

    def test_none_on_non_apk_or_missing_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            import zipfile
            junk = pathlib.Path(t) / "junk.bin"; junk.write_bytes(b"not a zip at all")
            self.assertIsNone(P.apk_package_id(junk))
            nomani = pathlib.Path(t) / "nomani.apk"
            with zipfile.ZipFile(nomani, "w") as z:
                z.writestr("classes.dex", b"x")
            self.assertIsNone(P.apk_package_id(nomani))


class TestAdbLaunch(unittest.TestCase):
    """Adb.launch / go_home / pkg_installed — the three primitives ③ Warm up is built from."""

    def test_launch_uses_monkey_launcher_intent(self):
        r = FakeRunner()
        a = Adb(runner=r)
        self.assertTrue(a.launch("org.ppsspp.ppsspp"))
        cmd = r.calls[-1][-1]
        self.assertIn("monkey -p org.ppsspp.ppsspp", cmd)
        self.assertIn("android.intent.category.LAUNCHER", cmd)

    def test_launch_false_when_monkey_finds_no_activity(self):
        class NoActivity(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                self.calls.append(list(args))
                if "monkey" in args[-1]:
                    # real monkey text when a package has no LAUNCHER activity: rc 0, but nothing injected
                    return 0, "** No activities found to run, monkey aborted.\n", ""
                return 0, "", ""
        a = Adb(runner=NoActivity())
        self.assertFalse(a.launch("com.no.ui.app"))

    def test_go_home_sends_home_intent(self):
        r = FakeRunner()
        self.assertTrue(Adb(runner=r).go_home())
        self.assertIn("android.intent.category.HOME", r.calls[-1][-1])

    def test_pkg_installed_reflects_pm_path(self):
        class PmPath(FakeRunner):
            def __init__(self, present):
                super().__init__()
                self.present = present
            def __call__(self, args, input_text=None, timeout=900):
                self.calls.append(list(args))
                if args[-1].startswith("pm path "):
                    return (0, "package:/data/app/base.apk\n", "") if self.present else (1, "", "")
                return 0, "", ""
        self.assertTrue(Adb(runner=PmPath(True)).pkg_installed("com.foo"))
        self.assertFalse(Adb(runner=PmPath(False)).pkg_installed("com.foo"))


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES", "XDG_RUNTIME_DIR")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_config_library_wins_over_default(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ.pop("CAS_PROFILES", None)
            # compare Path objects (not str): str(Path) renders \ on Windows, / on POSIX
            self.assertEqual(pathlib.Path(C.set_library("/mnt/nas/CAS Profiles")),
                             pathlib.Path("/mnt/nas/CAS Profiles"))
            self.assertEqual(C.load_config().get("library"), "/mnt/nas/CAS Profiles")

    def test_always_install_default_override_and_clear(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            # key absent -> default set
            self.assertEqual(
                C.always_install_pkgs(),
                frozenset({"com.valvesoftware.steamlink", "com.gamecove.gamecove_companion"}))
            # explicit override wins and is persisted sorted
            self.assertEqual(C.set_always_install_pkgs(["com.foo", "com.bar"]),
                             frozenset({"com.foo", "com.bar"}))
            self.assertEqual(C.load_config().get("always_install"), ["com.bar", "com.foo"])
            # a stored empty list DISABLES the feature (getter honors [])
            C.save_config({"always_install": []})
            self.assertEqual(C.always_install_pkgs(), frozenset())
            # setter with a falsy value CLEARS the override -> back to default (mirrors set_library)
            self.assertEqual(
                C.set_always_install_pkgs(None),
                frozenset({"com.valvesoftware.steamlink", "com.gamecove.gamecove_companion"}))
            self.assertNotIn("always_install", C.load_config())

    def test_always_install_setter_wraps_bare_string(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            # bare string is treated as a single package, not iterated into chars
            C.set_always_install_pkgs("com.solo")
            self.assertEqual(C.always_install_pkgs(), frozenset({"com.solo"}))

    def test_always_install_setter_none_clears_empty_disables(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            C.set_always_install_pkgs(["com.a", "com.b"])
            # empty list STORES [] -> disabled (getter returns empty, NOT the default set)
            self.assertEqual(C.set_always_install_pkgs([]), frozenset())
            self.assertEqual(C.load_config().get("always_install"), [])
            self.assertEqual(C.always_install_pkgs(), frozenset())
            # None CLEARS the override -> default set returns
            self.assertEqual(
                C.set_always_install_pkgs(None),
                frozenset({"com.valvesoftware.steamlink", "com.gamecove.gamecove_companion"}))
            self.assertNotIn("always_install", C.load_config())

    def test_store_window_toggle_roundtrip(self):
        # Mirrors the Managed APKs window's toggle_always: P.toggle_always_member ->
        # config.set_always_install_pkgs. Toggling a pkg ON then OFF is reflected by the getter.
        from cas import config as C
        from cas import profiles as P
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            C.set_always_install_pkgs(["com.a"])                      # known starting set
            C.set_always_install_pkgs(sorted(P.toggle_always_member(C.always_install_pkgs(), "com.b")))
            self.assertEqual(C.always_install_pkgs(), frozenset({"com.a", "com.b"}))   # com.b added
            C.set_always_install_pkgs(sorted(P.toggle_always_member(C.always_install_pkgs(), "com.b")))
            self.assertEqual(C.always_install_pkgs(), frozenset({"com.a"}))            # com.b removed

    def test_device_profiles_persist(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            self.assertEqual(C.get_device_profiles(), {})
            C.set_device_profile("2ee078bd", "retroid-pocket-6-512", manual=False)   # remembered auto-match
            C.set_device_profile("ABC123", "odin2-mini", manual=True)                # operator override
            dp = C.get_device_profiles()
            self.assertEqual(dp["2ee078bd"], {"profile": "retroid-pocket-6-512", "manual": False})
            self.assertEqual(dp["ABC123"], {"profile": "odin2-mini", "manual": True})
            C.set_device_profile("ABC123", None)                                     # forget one
            self.assertNotIn("ABC123", C.get_device_profiles())
            self.assertIn("2ee078bd", C.get_device_profiles())                        # the other survives

    def test_download_stats_average_and_tracking(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            self.assertIsNone(C.download_mbps())                 # nothing recorded yet
            C.record_download(100 * 1048576, 10, profile="rp6-512", serial="2ee078bd", model="Retroid Pocket 6")
            C.record_download(100 * 1048576, 10, profile="rp6-512", serial="2ee078bd")
            self.assertAlmostEqual(C.download_mbps(), 10.0, places=3)
            # the record carries which profile + device
            rec = C.download_stats()[-1]
            self.assertEqual(rec["profile"], "rp6-512")
            self.assertEqual(rec["serial"], "2ee078bd")
            # a slower sample for ANOTHER profile -> per-profile avg prefers the matching profile's history
            C.record_download(100 * 1048576, 50, profile="odin2-mini", serial="ABC123")  # 2 MB/s
            self.assertAlmostEqual(C.download_mbps("rp6-512"), 10.0, places=3)
            self.assertAlmostEqual(C.download_mbps("odin2-mini"), 2.0, places=3)
            C.record_download(0, 5)                              # ignored (no bytes)
            C.record_download(50 * 1048576, 0)                   # ignored (no time)
            self.assertAlmostEqual(C.download_mbps("rp6-512"), 10.0, places=3)

    def test_env_wins_over_config(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            C.set_library("/mnt/nas/lib")
            os.environ["CAS_PROFILES"] = "/tmp/override"
            self.assertEqual(pathlib.Path(C.library_root()), pathlib.Path("/tmp/override"))

    def test_corrupt_config_is_empty(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            cfg = pathlib.Path(t) / "cas-config.json"
            cfg.write_text("{ this is not json")
            os.environ["CAS_CONFIG"] = str(cfg)
            self.assertEqual(C.load_config(), {})

    def test_library_reachable(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ["CAS_PROFILES"] = t
            self.assertTrue(C.library_reachable())
            os.environ["CAS_PROFILES"] = str(pathlib.Path(t) / "nope")
            self.assertFalse(C.library_reachable())

    def test_machine_tag_sanitizes_hostname(self):
        from cas import config as C
        from unittest import mock
        with mock.patch("socket.gethostname", return_value="Bench 01/Room#2"):
            self.assertEqual(C.machine_tag(), "bench-01-room-2")
        with mock.patch("socket.gethostname", return_value=""):
            self.assertEqual(C.machine_tag(), "unknown")

    def test_history_filename_shape(self):
        from cas import config as C
        from unittest import mock
        with mock.patch.object(C, "machine_tag", lambda: "bench-01"):
            self.assertEqual(C.history_filename("download-history"),
                             "download-history.bench-01.jsonl")

    def test_es_media_src_set_get_clear(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ.pop("CAS_MEDIA", None)
            self.assertIsNone(C.es_media_src())                # default => SD mode (no push)
            C.set_es_media_src("/pc/ES-DE")
            self.assertEqual(C.es_media_src(), "/pc/ES-DE")
            C.set_es_media_src(None)                           # clear => back to SD mode
            self.assertIsNone(C.es_media_src())
            os.environ["CAS_MEDIA"] = "/env/wins"              # env overrides config
            C.set_es_media_src("/pc/ES-DE")
            self.assertEqual(C.es_media_src(), "/env/wins")
            os.environ.pop("CAS_MEDIA", None)

    def test_library_root_local_default(self):
        from cas import config as C, APPDIR
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")
            os.environ.pop("CAS_PROFILES", None)
            self.assertEqual(C.library_root(), APPDIR / "data" / "profiles")

    def test_firmware_dir_ignores_stale_override(self):
        from cas import config as C
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            cfgp = pathlib.Path(t) / "cas-config.json"
            cfgp.write_text('{"firmware_dir": "/mnt/gamecove/does-not-exist/_firmware"}')
            os.environ["CAS_CONFIG"] = str(cfgp)
            os.environ.pop("CAS_PROFILES", None)
            lib = pathlib.Path(t) / "lib"; lib.mkdir()
            with mock.patch.object(C, "library_root", lambda: lib):
                self.assertEqual(C.firmware_dir(), lib / "_firmware")     # stale override ignored

    def test_firmware_dir_honors_existing_override(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            cfgp = pathlib.Path(t) / "cas-config.json"
            real = pathlib.Path(t) / "fw"; real.mkdir()
            cfgp.write_text('{"firmware_dir": %s}' % __import__("json").dumps(str(real)))
            os.environ["CAS_CONFIG"] = str(cfgp)
            self.assertEqual(C.firmware_dir(), real)

    def test_cores_dir_prefers_library_then_falls_back_to_appdir(self):
        # RetroArch cores are sourced from the CAS LIBRARY (library_root()/retroarch-cores), so the ~2.4GB
        # set lives WITH the profiles on the library drive — not beside the exe. Falls back to APPDIR/data/
        # retroarch-cores only when the library has no .so.
        from cas import config as C, APPDIR
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")
            os.environ.pop("CAS_PROFILES", None)
            lib = pathlib.Path(t) / "lib"; lib.mkdir()
            with mock.patch.object(C, "library_root", lambda: lib):
                self.assertEqual(C.cores_dir(), APPDIR / "data" / "retroarch-cores")  # library empty -> fallback
                cores = lib / "retroarch-cores"; cores.mkdir()
                (cores / "snes9x_libretro_android.so").write_bytes(b"MZ")
                self.assertEqual(C.cores_dir(), cores)                                # library populated -> preferred

    def test_cores_dir_honors_existing_override(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            cfgp = pathlib.Path(t) / "cas-config.json"
            real = pathlib.Path(t) / "cores"; real.mkdir()
            cfgp.write_text('{"cores_dir": %s}' % __import__("json").dumps(str(real)))
            os.environ["CAS_CONFIG"] = str(cfgp)
            self.assertEqual(C.cores_dir(), real)

    def test_apk_store_defaults_under_library(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "missing.json")
            os.environ["CAS_PROFILES"] = str(pathlib.Path(t) / "lib")
            self.assertEqual(C.apk_store_dir(), pathlib.Path(t) / "lib" / "_apks")

    def test_apk_store_override_honored_only_if_exists(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            os.environ["CAS_PROFILES"] = str(pathlib.Path(t) / "lib")
            store = pathlib.Path(t) / "store"; store.mkdir()
            C.set_apk_store(str(store))
            self.assertEqual(C.apk_store_dir(), store)                       # exists -> honored
            C.set_apk_store(str(pathlib.Path(t) / "gone"))                   # nonexistent override
            self.assertEqual(C.apk_store_dir(), pathlib.Path(t) / "lib" / "_apks")  # ignored

    def test_warmup_dwell_default_and_override(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            self.assertEqual(C.warmup_dwell_s(), 1.0)          # key absent -> default
            C.save_config({"warmup_dwell_s": 10})
            self.assertEqual(C.warmup_dwell_s(), 10.0)         # int is coerced to float
            C.save_config({"warmup_dwell_s": "bogus"})         # unparseable -> default, never crash
            self.assertEqual(C.warmup_dwell_s(), 1.0)
            C.save_config({"warmup_dwell_s": -5})              # negative is clamped to 0
            self.assertEqual(C.warmup_dwell_s(), 0.0)

    def test_warmup_skip_pkgs_default_override_and_empty(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            # key absent -> Magisk only (a host tool, never a shipped app)
            self.assertEqual(C.warmup_skip_pkgs(), frozenset({"com.topjohnwu.magisk"}))
            C.save_config({"warmup_skip_pkgs": ["com.foo", "com.bar"]})
            self.assertEqual(C.warmup_skip_pkgs(), frozenset({"com.foo", "com.bar"}))
            C.save_config({"warmup_skip_pkgs": []})            # stored [] -> skip NOTHING
            self.assertEqual(C.warmup_skip_pkgs(), frozenset())

    def test_warmup_settle_default_and_override(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            self.assertEqual(C.warmup_settle_s(), 10.0)        # key absent -> default
            C.save_config({"warmup_settle_s": 45})
            self.assertEqual(C.warmup_settle_s(), 45.0)        # int is coerced to float
            C.save_config({"warmup_settle_s": "bogus"})        # unparseable -> default, never crash
            self.assertEqual(C.warmup_settle_s(), 10.0)
            C.save_config({"warmup_settle_s": -5})             # negative is clamped to 0
            self.assertEqual(C.warmup_settle_s(), 0.0)


class TestReleaseToken(unittest.TestCase):
    def test_default_token_when_no_override(self):
        from cas import config as C
        orig = C.load_config
        C.load_config = lambda: {}
        try:
            self.assertEqual(C.get_release_token(), C.RELEASE_TOKEN_DEFAULT)
            self.assertEqual(C.RELEASE_TOKEN_DEFAULT, "gc-release-7f3a9c2e")
        finally:
            C.load_config = orig

    def test_operator_override_wins(self):
        from cas import config as C
        orig = C.load_config
        C.load_config = lambda: {"release_token": "custom-xyz"}
        try:
            self.assertEqual(C.get_release_token(), "custom-xyz")
        finally:
            C.load_config = orig


class TestProvision(unittest.TestCase):
    def setUp(self):
        # CAS_CONFIG isolation (regression guard — see Finding 2): several tests below run provision()'s
        # REAL (non-dry) push path to completion, which calls config.record_download() and writes
        # straight through to whatever CAS_CONFIG resolves to. Some tests here isolate it themselves
        # (with a bare del/pop that assumes it was previously unset); at least one used to not isolate it
        # at ALL, corrupting the operator's real cas-config.json on every suite run. This class-level
        # save/restore is the backstop: it captures the module default from setUpModule() and reinstates
        # it after EVERY test in this class, regardless of what the test body did to the env var.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    def test_provision_runs_restore_and_reboot(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner()
            logs = []
            ok = PV.provision(Adb(runner=r), prof, log=logs.append, dry_push=True)
            self.assertTrue(ok)
            joined = "\n".join(r.cmds())
            self.assertIn("restore.sh", joined)
            self.assertIn("CAS_MANIFEST", joined)
            self.assertTrue(any(c[-1] == "reboot" for c in r.calls))

    def test_provision_push_failure_logs_adb_reason(self):
        # When a payload push fails, the abort must carry adb's actual error text (e.g. 'device
        # offline') so the operator sees WHY — not a blind 'PUSH FAILED'. Regression for the run
        # where com.retroarch.aarch64 died 3x with no reason surfaced.
        class OfflineOnPush(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                if "push" in args:
                    return 1, "", "adb: error: failed to read: device offline"
                return super().__call__(args, input_text, timeout)
        logs = []
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            from unittest import mock
            with mock.patch("time.sleep", lambda *a, **k: None):
                ok = PV.provision(Adb(runner=OfflineOnPush()), prof, log=logs.append, dry_push=False)
        self.assertFalse(ok)
        blob = "\n".join(logs)
        self.assertIn("device offline", blob)       # the real reason is surfaced...
        self.assertIn("PUSH FAILED", blob)           # ...alongside the abort

    def test_provision_sd_media_is_default(self):
        # no es_media_src => 'sd' mode: restore gets CAS_ES_MEDIA=sd and NO box-art push happens.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)                              # includes org.es_de.frontend
            r = FakeRunner()
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)
            self.assertTrue(ok)
            joined = "\n".join(r.cmds())
            self.assertIn("CAS_ES_MEDIA=sd", joined)
            self.assertIn("restore.sh", joined)

    def test_provision_internal_media_sets_mode(self):
        # es_media_src set => 'internal' mode: restore gets CAS_ES_MEDIA=internal.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner()
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True,
                              es_media_src=str(pathlib.Path(t) / "ES-DE"))
            self.assertTrue(ok)
            self.assertIn("CAS_ES_MEDIA=internal", "\n".join(r.cmds()))

    def test_provision_refuses_golden(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner(golden=True)
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)
            self.assertFalse(ok)
            self.assertNotIn("restore.sh", "\n".join(r.cmds()))

    def test_provision_requires_root(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            ok = PV.provision(Adb(runner=FakeRunner(root=False)), prof, log=lambda m: None, dry_push=True)
            self.assertFalse(ok)

    def test_provision_blocked_su_reports_no_root_not_golden(self):
        # Real-world failure: Magisk's grant prompt was never tapped, so every `su` BLOCKS -> timeout (124).
        # is_golden() is fail-closed, so if it runs FIRST it wrongly cries "golden lock". The user must see
        # the real cause — no root — so they grant Magisk/root instead of thinking the unit is sealed.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            logs = []
            ok = PV.provision(Adb(runner=FakeRunner(su_blocked=True)), prof, log=logs.append, dry_push=True)
            self.assertFalse(ok)
            blob = " ".join(logs).lower()
            self.assertIn("no root", blob)
            self.assertNotIn("golden", blob)

    def test_provision_all_detail_is_real_reason_not_profile_name(self):
        # The mini-report's per-device detail must carry the ACTUAL failure reason, not just the profile
        # name (which told the user nothing about WHY it failed).
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            res = PV.provision_all(lambda s: Adb(runner=FakeRunner(su_blocked=True)),
                                   [("ABC123", "device")], profile=prof, log=lambda m: None)
            status, detail = res["ABC123"][:2]        # failures now carry a 3rd Recovery element
            self.assertEqual(status, "fail")
            self.assertNotEqual(detail, prof.name)
            self.assertIn("no root", detail.lower())

    def test_provision_refuses_no_sd(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner(sd=False)
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)
            self.assertFalse(ok)
            self.assertNotIn("restore.sh", "\n".join(r.cmds()))   # never reached restore

    def test_provision_refuses_invalid_payload(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            (pathlib.Path(prof.payload) / "global.meta").unlink()       # corrupt the payload
            r = FakeRunner()
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None, dry_push=True)
            self.assertFalse(ok)
            self.assertNotIn("restore.sh", "\n".join(r.cmds()))

    def test_provision_aborts_on_push_failure(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            r = FakeRunner(push_ok=False)                              # a push fails -> must abort
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)  # real push path
            self.assertFalse(ok)
            self.assertNotIn("restore.sh", "\n".join(r.cmds()))        # never reached restore

    def test_provision_never_pushes_a_directory(self):
        # ROOT-CAUSE REGRESSION — Windows Download "no APK in payload" (Retroid Pocket 6, v0.3.4):
        # `adb.exe push <directory>` reports SUCCESS but transfers 0 files on Windows (single-FILE pushes
        # are fine — see adb._local). The forward-slash workaround alone was insufficient, so the captured
        # module dirs never landed and restore.sh failed with "no APK in payload" for every app while the
        # single-file metas (global.meta/pkglist) DID arrive. Fix: move every DIRECTORY over as ONE stored
        # tar + on-device untar, exactly like push_es_media. Guard: provision must NEVER hand adb a
        # directory to push, and the captured payload must arrive as a tar that is unpacked on the device.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)                              # 3 captured module dirs, each apk + data.tar
            r = FakeRunner()
            ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)   # real (non-dry) push path
            self.assertTrue(ok)
            for c in r.calls:                                   # (1) no push source is a directory
                if "push" in c:
                    src = c[c.index("push") + 1]
                    self.assertFalse(os.path.isdir(src),
                                     f"pushed a DIRECTORY (0-byte on Windows): {src}")
            joined = "\n".join(r.cmds())                        # (2) payload arrives as a tar, unpacked on device
            self.assertIn("tar -xf", joined)
            self.assertIn("/data/local/tmp/cas/payload", joined)

    def test_provision_pushes_the_captured_wifi_store(self):
        # ROOT-CAUSE REGRESSION — "save wifi not working": capture.sh saves the golden's WifiConfigStore.xml
        # into golden_root_payload/wifi/, and restore.sh's restore_wifi clones it onto the fresh unit — but
        # the Download push loop never sent the wifi/ dir to the device, so restore_wifi ALWAYS found nothing
        # ("wifi: no wifi in payload — skip") and every unit shipped without the shop network, on EVERY
        # platform. Guard: the captured wifi/ dir must be delivered, and as a TAR via _push_dir (never
        # `adb push <dir>`, which lands 0 files on Windows) — exactly like settings/homescreen/gamelauncher.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            (prof.payload / "wifi").mkdir()
            (prof.payload / "wifi" / "WifiConfigStore.xml").write_text(
                '<WifiConfigStore><Network><string name="SSID">&quot;Shop&quot;</string></Network></WifiConfigStore>')
            pushed_dir_names = []
            real_push_dir = PV._push_dir
            def spy(adb, push, src, dev_parent, log, arcname=None):
                pushed_dir_names.append(pathlib.Path(src).name)
                return real_push_dir(adb, push, src, dev_parent, log, arcname)
            r = FakeRunner()
            with patch.object(PV, "_push_dir", spy):
                ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)   # real (non-dry) push path
            self.assertTrue(ok)
            self.assertIn("wifi", pushed_dir_names)             # the captured wifi store WAS delivered...
            for c in r.calls:                                   # ...and never as a raw directory push
                if "push" in c:
                    src = c[c.index("push") + 1]
                    self.assertFalse(os.path.isdir(src),
                                     f"pushed a DIRECTORY (0-byte on Windows): {src}")

    def test_provision_all_failure_carries_recovery_guidance(self):
        # A failed Download must return a 3-tuple whose 3rd element is a Recovery, and log the DO-NEXT
        # block live. Deterministic failure: no SD -> provision() refuses early.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)
            logs = []
            r = FakeRunner(sd=False)                             # provision() refuses: "no SD card"
            res = PV.provision_all(lambda s: Adb(runner=r), [("MQ66TEST", "device")],
                                   root=t, profile=prof, log=logs.append, parallel=False)
            entry = res["MQ66TEST"]
            self.assertEqual(entry[0], "fail")
            self.assertTrue(len(entry) > 2, "no Recovery element on the failing result")
            rec = entry[2]
            self.assertIsNotNone(rec)
            self.assertTrue(rec.steps)
            self.assertIn("DO NEXT", "\n".join(logs))           # the live log carried the guidance block

    def test_seal_all_sealed_then_dropped_is_success_not_attention(self):
        # Lock's by-design adb disconnect: if seal() logged its "SEALED" completion marker but then
        # returned False (adb dropped as the unit sealed), that is SUCCESS — never a scary attention popup.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)

            def fake_seal(adb, fb, stock, log=print, **k):
                log("hid Developer options + disabled USB debugging. Device is SEALED — "
                    "adb will now disconnect. Done.")
                return False                                    # connection dropped right after the seal

            with patch.object(PV, "seal", fake_seal):
                r = FakeRunner()
                res = PV.seal_all(lambda s: Adb(runner=r), lambda s: Fastboot(runner=r),
                                  [("RP6X", "device")], profiles_root=t, appdir=t, profile=prof,
                                  log=lambda m: None, parallel=False)
            entry = res["RP6X"]
            self.assertEqual(entry[0], "ok")                    # sealed-then-dropped = success
            self.assertTrue(len(entry) > 2 and entry[2] is not None)
            self.assertFalse(entry[2].needs_attention)          # excluded from the attention popup

    def test_push_es_media_packs_one_archive(self):
        # Universal fast path: pack downloaded_media into ONE tar, push that single file, unpack it on the
        # device, delete it. Verifies we do NOT do a slow per-file directory push and that both the
        # device-side archive AND the PC-side archive are cleaned up.
        with tempfile.TemporaryDirectory() as t:
            media = pathlib.Path(t) / "ES-DE" / "downloaded_media"
            (media / "nes" / "covers").mkdir(parents=True)
            (media / "nes" / "covers" / "game.jpg").write_bytes(b"img")
            r = FakeRunner()
            ok = PV.push_es_media(Adb(runner=r), log=lambda m: None, media_src=str(media))
            self.assertTrue(ok)
            joined = "\n".join(r.cmds())
            self.assertIn("tar -xf /data/local/tmp/cas_es_media.tar", joined)   # unpacked on device
            self.assertIn("rm -f /data/local/tmp/cas_es_media.tar", joined)     # device archive removed
            pushes = [c for c in r.calls if "push" in c]
            self.assertEqual(len(pushes), 1)                                    # ONE file, not a tree
            self.assertTrue(pushes[0][-2].endswith(".tar"))                     # src is the archive
            self.assertEqual(pushes[0][-1], "/data/local/tmp/cas_es_media.tar")
            self.assertEqual(list(pathlib.Path(t).rglob("cas_es_media_*.tar")), [])  # PC archive cleaned up

    def test_push_es_media_pushes_premade_archive(self):
        # If the PC source is ALREADY a single archive, push it AS-IS (no on-the-fly packing), pick the
        # gzip unpack flag for .tar.gz, and never delete the operator's stored file.
        with tempfile.TemporaryDirectory() as t:
            md = pathlib.Path(t) / "downloaded_media"
            (md / "nes").mkdir(parents=True)
            (md / "nes" / "a.jpg").write_bytes(b"x")
            arc = pathlib.Path(t) / "es-de-media.tar.gz"
            with tarfile.open(arc, "w:gz") as tar:
                tar.add(str(md), arcname="downloaded_media")
            r = FakeRunner()
            ok = PV.push_es_media(Adb(runner=r), log=lambda m: None, media_src=str(arc))
            self.assertTrue(ok)
            joined = "\n".join(r.cmds())
            self.assertIn("tar -xzf /data/local/tmp/cas_es_media.tar", joined)  # gzip unpack flag
            pushes = [c for c in r.calls if "push" in c]
            self.assertEqual(len(pushes), 1)                                    # pushed ONE file
            self.assertTrue(pushes[0][-2].endswith(".tar.gz"))                  # the archive itself, as-is
            self.assertTrue(arc.exists())                                       # operator's file untouched

    def test_push_es_media_skips_when_present(self):
        # A re-provision must NOT re-pack/re-push 12 GB if the device already has the box art.
        with tempfile.TemporaryDirectory() as t:
            media = pathlib.Path(t) / "downloaded_media"
            (media / "nes").mkdir(parents=True)
            (media / "nes" / "a.jpg").write_bytes(b"x")

            class Present(FakeRunner):
                def __call__(self, args, input_text=None, timeout=900):
                    if "shell" in args and args[-1].startswith("ls /storage"):
                        return 0, "/storage/emulated/0/ES-DE/downloaded_media\n", ""
                    return super().__call__(args, input_text, timeout)
            r = Present()
            ok = PV.push_es_media(Adb(runner=r), log=lambda m: None, media_src=str(media))
            self.assertTrue(ok)
            self.assertFalse([c for c in r.calls if "push" in c])              # nothing pushed
            self.assertNotIn("tar -xf", "\n".join(r.cmds()))                   # nothing unpacked

    def test_provision_all_isolates_exception(self):
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "odin2mini", "Odin2 ?Mini")

            def boom(s):
                raise RuntimeError("device fault")
            res = PV.provision_all(boom, [("ABC123", "device")], root=t, log=lambda m: None)
            self.assertEqual(res["ABC123"][0], "error")                # isolated, not raised

    def test_provision_all_wait_boot_fails_a_unit_that_never_reboots(self):
        # In a Download->Lock chain (wait_boot=True) each worker blocks on the post-Download reboot: a unit
        # that comes back is ok; one that never boots back is FAILED so Lock never starts on an offline unit.
        from unittest.mock import patch
        prof = type("P", (), {"name": "p"})()

        class FakeAdb:
            def __init__(self, serial, back):
                self.serial = serial; self.cancel = None; self._back = back
            def wait_boot(self, on_tick=None, timeout=180):
                return self._back
        with tempfile.TemporaryDirectory() as t, patch.object(PV, "provision", lambda *a, **k: True):
            res = PV.provision_all(lambda s: FakeAdb(s, back=(s == "BACK")),
                                   [("BACK", "device"), ("STUCK", "device")], root=t, log=lambda m: None,
                                   profile_map={"BACK": prof, "STUCK": prof}, wait_boot=True)
        self.assertEqual(res["BACK"][0], "ok")
        self.assertEqual(res["STUCK"][0], "fail")
        self.assertIn("did not boot back", res["STUCK"][1])

    def test_batch_auto_matches_and_skips_unauthorized(self):
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, "odin2mini", "Odin2 ?Mini")
            shared = FakeRunner()
            res = PV.provision_all(
                lambda s: Adb(serial=s, runner=shared),
                [("ABC123", "device"), ("DEF456", "unauthorized")],
                root=t, log=lambda m: None,
            )
            self.assertEqual(res["ABC123"], ("ok", "odin2mini"))
            self.assertEqual(res["DEF456"][0], "skip")

    def test_download_run_logged_to_library(self):
        # the WHOLE Download is recorded (total length + each device/profile) to the library, every run
        import json
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")   # isolate: no log_dir override
            try:
                make_profile(t, "odin2mini", "Odin2 ?Mini")
                res = PV.provision_all(lambda s: Adb(serial=s, runner=FakeRunner()),
                                       [("ABC123", "device")], root=t, log=lambda m: None,
                                       profile_map={"ABC123": P.Profile(pathlib.Path(t) / "odin2mini")})
                self.assertEqual(res["ABC123"][0], "ok")
                hist = pathlib.Path(t) / C.history_filename("download-history")
                self.assertTrue(hist.exists())              # written into the library dir (no log_dir set)
                rec = json.loads(hist.read_text().splitlines()[-1])
                self.assertEqual(rec["ok"], 1)
                self.assertIn("total_bytes", rec)
                self.assertIn("total_secs", rec)
                self.assertEqual(rec["devices"][0]["serial"], "ABC123")
                self.assertEqual(rec["devices"][0]["profile"], "odin2mini")
            finally:
                os.environ.pop("CAS_CONFIG", None)

    def test_append_history_writes_jsonl(self):
        # the shared run-history appender used by BOTH download-history and save-history
        import json
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")   # isolate: no log_dir set
            try:
                PV._append_history(t, "save-history", {"profile": "rp6-512", "bytes": 123}, log=lambda m: None)
                PV._append_history(t, "save-history", {"profile": "odin2", "bytes": 456}, log=lambda m: None)
                lines = (pathlib.Path(t) / C.history_filename("save-history")).read_text().splitlines()
                self.assertEqual(len(lines), 2)
                self.assertEqual(json.loads(lines[0])["profile"], "rp6-512")
                self.assertEqual(json.loads(lines[1])["bytes"], 456)
            finally:
                os.environ.pop("CAS_CONFIG", None)

    def test_append_history_routes_to_log_dir(self):
        # A configured + reachable shared log_dir receives the run history, NOT the library root — so logs
        # centralize across benches while goldens stay on a fast LOCAL library. An unreachable log_dir falls
        # back to the library root so a run is never lost.
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            lib = pathlib.Path(t) / "lib"; lib.mkdir()
            alt = pathlib.Path(t) / "alt"; alt.mkdir()
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            try:
                C.set_log_dir(str(alt))
                PV._append_history(str(lib), "download-history", {"ok": 1}, log=lambda m: None)
                self.assertTrue((alt / C.history_filename("download-history")).exists())   # log_dir override
                self.assertFalse((lib / C.history_filename("download-history")).exists())  # NOT the library root
                C.set_log_dir(str(pathlib.Path(t) / "gone"))                               # unreachable -> fallback
                PV._append_history(str(lib), "save-history", {"ok": 1}, log=lambda m: None)
                self.assertTrue((lib / C.history_filename("save-history")).exists())       # fell back to library root
            finally:
                os.environ.pop("CAS_CONFIG", None)

    def test_capture_to_pc_invokes_capture(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t,
                             log=lambda m: None, dry_pull=True)
            self.assertIn("capture.sh", "\n".join(r.cmds()))
            self.assertIn("CAS_OUT=/data/local/tmp", "\n".join(r.cmds()))

    def test_capture_to_pc_passes_capture_manifest_when_present(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            pdir = pathlib.Path(t) / "newprof"; pdir.mkdir()
            (pdir / "capture-manifest").write_text("# cap\ncom.foo\n")
            PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t, log=lambda m: None, dry_pull=True)
            joined = "\n".join(r.cmds())
            self.assertIn("CAS_MANIFEST=/data/local/tmp/cas_scripts/capture-manifest", joined)

    def test_capture_to_pc_no_manifest_captures_all(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t, log=lambda m: None, dry_pull=True)
            self.assertNotIn("CAS_MANIFEST=", "\n".join(r.cmds()))   # back-compat: capture-all

    def test_capture_to_pc_aborts_fast_when_not_rooted(self):
        # Save clones the golden's ROOT payload -> if the unit isn't rooted, fail fast BEFORE pushing
        # scripts / running capture, so the operator isn't left waiting on a doomed capture.
        with tempfile.TemporaryDirectory() as t:
            r = FakeRunner(root=False)
            msgs = []
            ok = PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t, log=msgs.append)
            self.assertFalse(ok)
            self.assertTrue(any("root" in m.lower() for m in msgs), f"expected not-rooted message; got {msgs}")
            self.assertNotIn("capture.sh", "\n".join(r.cmds()))          # aborted before capturing
            self.assertFalse(any("push" in c for c in r.cmds()))         # aborted before pushing scripts

    @staticmethod
    def _golden_tar_bytes():
        """A minimal but COMPLETE golden payload tar (passes capture_to_pc's global.meta/pkglist check)."""
        import io, tarfile as _tf
        buf = io.BytesIO()
        with _tf.open(fileobj=buf, mode="w") as tar:
            for nm, data in (("global.meta", b"golden_serial=9C33-6BBD\n"),
                             ("pkglist.txt", b"com.foo\n"),
                             ("com.foo/apk/base.apk", b"x"),
                             ("com.foo/data.tar", b"x")):
                ti = _tf.TarInfo(nm); ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def test_pack_cmd_is_one_command_no_shell_metacharacters(self):
        # ROOT CAUSE of the 'on-device pack of /data/local/tmp/cas_cap failed (rc=1)' abort (RP6, 2.1G
        # golden, 90G free — never a space problem): adb SPACE-JOINS the argv after shell/exec-out, so the
        # DEVICE's shell parsed the line before `su` ever saw it. `su -c cd DIR && tar -cf - .` therefore
        # ran ONLY the `cd` under su (in su's own subshell, moving nothing) and then ran `tar` as the
        # unprivileged SHELL user from adb's CWD `/` — archiving the filesystem ROOT until it hit
        # "tar: can't open './sys/wifi/feature': Permission denied" -> "short read" -> rc 1.
        # The pack must stay ONE command using `tar -C <dir>`, with NO shell metacharacters to steal.
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            Adb(serial="S1", runner=r).su_pack_to_file(PV.TMPCAP, pathlib.Path(t) / "p.tar",
                                                       0, lambda m: None)
        pack = [c for c in r.calls if "exec-out" in c]
        self.assertEqual(len(pack), 1, f"expected exactly one exec-out pack call; got {r.calls}")
        cmd = pack[0][-1]
        self.assertEqual(cmd, f"tar -C {PV.TMPCAP} -cf - .")
        for meta in ("&&", "||", ";", "|", "cd ", '"', "'"):
            self.assertNotIn(meta, cmd,
                             f"`su -c` space-joining lets the DEVICE shell steal {meta!r}: {cmd!r}")

    def test_capture_streams_one_tar_never_stages_a_device_copy(self):
        # UNIVERSAL transfer (Windows + Linux): Save must move the golden as ONE archive that STREAMS off
        # the device (`adb exec-out ... tar -C <dir> -cf - .` -> PC file -> PC-unpack), NEVER `adb pull
        # <dir>` (a directory pull can silently drop files on Windows and write an incomplete golden) and
        # NEVER by staging a second full-size `<dir>.tar` on the device first (a needless full extra copy
        # on the same partition). Guards: the pack streams to stdout (`-`), no device-side tar file is
        # created, no directory is pulled, and the profile unpacks whole.
        class _CapRunner(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                if "exec-out" in args:                   # the pack streams the tar to stdout -> return BYTES
                    self.calls.append(list(args))
                    return 0, TestProvision._golden_tar_bytes(), ""
                return super().__call__(args, input_text, timeout)
        with tempfile.TemporaryDirectory() as t:
            r = _CapRunner()
            ok = PV.capture_to_pc(Adb(runner=r), "newprof", "20260616", root=t, log=lambda m: None)
            self.assertTrue(ok)
            payload = pathlib.Path(t) / "newprof" / "golden_root_payload"
            self.assertTrue((payload / "global.meta").exists())              # unpacked on the PC
            self.assertTrue((payload / "com.foo" / "apk" / "base.apk").exists())
            for c in r.calls:
                self.assertNotIn("pull", c, f"pulled a directory/file instead of streaming: {c}")
            cmds = r.cmds()
            self.assertTrue(any("exec-out" in c and f"tar -C {PV.TMPCAP} -cf - ." in c for c in cmds),
                            f"pack must stream `tar -C <dir> -cf - .` via exec-out; got {cmds}")
            for c in cmds:                               # never write a device-side staging archive
                self.assertNotIn(f"tar -cf {PV.TMPCAP}.tar", c,
                                 f"staged a full second copy on the device: {c}")

    def test_pull_dir_surfaces_device_reason_on_pack_failure(self):
        # The abort must say WHY: the device stderr AND a free-space line — not the old opaque 'rc=1'.
        # (Regression: stderr was discarded, which is exactly why this bug hid as a space/'pack' mystery.)
        class _FailRunner(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                if "exec-out" in args:                   # the real observed failure, verbatim
                    self.calls.append(list(args))
                    return 1, b"", ("tar: can't open './sys/wifi/feature': Permission denied\n"
                                    "tar: short read")
                if "shell" in args and "/debug_ramdisk/su" in args and "df -h" in args[-1]:
                    self.calls.append(list(args))
                    return 0, "Filesystem  Size  Used Avail Use% Mounted on\n/dev/dm-14 101G 11G 90G 11% /data", ""
                return super().__call__(args, input_text, timeout)
        with tempfile.TemporaryDirectory() as t:
            r = _FailRunner()
            msgs = []
            ok = PV._pull_dir(Adb(runner=r), PV.TMPCAP, pathlib.Path(t) / "in", msgs.append)
            self.assertFalse(ok)
            blob = "\n".join(msgs)
            self.assertIn("short read", blob)                                # the device's real reason
            self.assertIn("90G", blob)                                       # free-space context

    def test_seed_default_manifest_populates_empty_placeholder(self):
        # A 'New profile' leaves a placeholder manifest with NO app lines; after the first capture the
        # selection must be seeded from the captured app set (both axes on) so the Apps tab shows the
        # downloaded apps ticked — and Download has apps to restore.
        with tempfile.TemporaryDirectory() as t:
            pdir = pathlib.Path(t) / "mangmi-air-x-256"
            (pdir / "golden_root_payload").mkdir(parents=True)
            (pdir / "golden_root_payload" / "pkglist.txt").write_text("com.a\ncom.b\n")
            (pdir / "manifest").write_text("# mangmi-air-x-256 (empty — capture a golden to populate)\n")
            PV.seed_default_manifest(pdir, "mangmi-air-x-256")
            self.assertEqual(P.manifest_pkgs(pdir / "manifest"), ["com.a", "com.b"])
            self.assertEqual(P.manifest_axes(pdir / "manifest"),
                             {"com.a": (True, True), "com.b": (True, True)})

    def test_seed_default_manifest_preserves_existing_selection(self):
        # If the operator already refined a real selection (manifest has app lines), seeding must NOT
        # clobber it on a re-capture.
        with tempfile.TemporaryDirectory() as t:
            pdir = pathlib.Path(t) / "prof"
            (pdir / "golden_root_payload").mkdir(parents=True)
            (pdir / "golden_root_payload" / "pkglist.txt").write_text("com.a\ncom.b\n")
            (pdir / "manifest").write_text("# prof\ncom.a config\n@settings on\n")
            PV.seed_default_manifest(pdir, "prof")
            self.assertEqual(P.manifest_pkgs(pdir / "manifest"), ["com.a"])          # unchanged
            self.assertEqual(P.manifest_axes(pdir / "manifest"), {"com.a": (False, True)})

    def test_seed_default_manifest_follows_capture_flags(self):
        # The Download defaults FOLLOW the Save modal's behavior choices (the capture-manifest @flags):
        # what the operator unticked at Save (e.g. hardening) pre-fills Download as off; missing = on.
        with tempfile.TemporaryDirectory() as t:
            pdir = pathlib.Path(t) / "prof"
            (pdir / "golden_root_payload").mkdir(parents=True)
            (pdir / "golden_root_payload" / "pkglist.txt").write_text("com.a\n")
            (pdir / "capture-manifest").write_text(
                "# prof capture\ncom.a\n@hardening off\n@settings off\n@gamelauncher on\n")
            PV.seed_default_manifest(pdir, "prof")
            f = P.manifest_flags(pdir / "manifest")
            self.assertEqual(f["hardening"], "off")     # Save untick propagates
            self.assertEqual(f["settings"], "off")      # Save untick propagates
            self.assertEqual(f["grants"], "on")         # absent in capture-manifest -> default on
            self.assertEqual(f["gamelauncher"], "on")   # explicit on carried through

    def test_patch_init_boot_on_device_pushes_toolkit_and_pulls(self):
        # On-device patch: push the aarch64 toolkit + the stock init_boot, run boot_patch.sh, pull the
        # patched new-boot.img back to the PC. No root needed (it only rewrites the image file).
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            stock = pathlib.Path(t) / "stock.img"
            stock.write_bytes(b"x")
            ok = PV.patch_init_boot_on_device(Adb(runner=r), stock, pathlib.Path(t) / "patched.img",
                                              log=lambda m: None)
        self.assertTrue(ok)
        cmds = "\n".join(r.cmds())
        self.assertIn("boot_patch.sh", cmds)              # ran Magisk's patcher on the device
        self.assertIn("push", cmds)                       # pushed toolkit + stock
        self.assertIn("pull", cmds)                       # pulled new-boot.img back

    def test_patch_init_boot_on_device_fails_without_sentinel(self):
        # No CAS_PATCH_OK in boot_patch.sh output -> treat as failure (don't flash a non-patched image).
        class _NoPatch(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                if "shell" in args and "boot_patch.sh" in args[-1]:
                    return 1, "- aborting\n", "magiskboot: not executable"
                return super().__call__(args, input_text, timeout)
        with tempfile.TemporaryDirectory() as t:
            stock = pathlib.Path(t) / "stock.img"
            stock.write_bytes(b"x")
            ok = PV.patch_init_boot_on_device(Adb(runner=_NoPatch()), stock,
                                              pathlib.Path(t) / "patched.img", log=lambda m: None)
        self.assertFalse(ok)

    def test_patch_injects_boot_grant_and_pulls_cas_boot(self):
        # With bake_boot_grant on (default), the overlay.d payload is pushed, magiskboot injects it,
        # and the repacked cas-boot.img is what gets pulled to the PC.
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            stock = pathlib.Path(t) / "stock.img"; stock.write_bytes(b"x")
            os.environ["CAS_CONFIG"] = os.path.join(t, "absent.json")  # default -> bake on
            try:
                ok = PV.patch_init_boot_on_device(Adb(runner=r), stock,
                                                  pathlib.Path(t) / "patched.img", log=lambda *_: None)
            finally:
                del os.environ["CAS_CONFIG"]
        self.assertTrue(ok)
        cmds = "\n".join(r.cmds())
        self.assertIn("overlay.d/cas-grant.sh", cmds)      # cpio added the script
        self.assertIn("overlay.d/init.cas-grant.rc", cmds) # cpio added the rc
        self.assertIn("magiskboot repack new-boot.img cas-boot.img", cmds)
        # r.calls records the FULL adb argv [adb, "pull", src, dst] (no serial in tests), so the verb
        # is c[1] and the pulled SOURCE image is c[2] — asserting c[2] pins cas-boot.img vs new-boot.img.
        self.assertTrue(any(c[1] == "pull" and c[2].endswith("cas-boot.img") for c in r.calls))

    def test_patch_inject_failure_falls_back_to_plain_image(self):
        # If the inject chain never emits CAS_INJECT_OK, the patch still succeeds by pulling the plain
        # patched new-boot.img (never a regression vs. today).
        class _NoInject(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                if "shell" in args and "CAS_INJECT_OK" in args[-1]:
                    return 1, "- unpack failed\n", "magiskboot: bad ramdisk"
                return super().__call__(args, input_text, timeout)
        r = _NoInject()
        with tempfile.TemporaryDirectory() as t:
            stock = pathlib.Path(t) / "stock.img"; stock.write_bytes(b"x")
            os.environ["CAS_CONFIG"] = os.path.join(t, "absent.json")
            try:
                ok = PV.patch_init_boot_on_device(Adb(runner=r), stock,
                                                  pathlib.Path(t) / "patched.img", log=lambda *_: None)
            finally:
                del os.environ["CAS_CONFIG"]
        self.assertTrue(ok)
        self.assertTrue(any(c[1] == "pull" and c[2].endswith("new-boot.img") for c in r.calls))
        self.assertFalse(any(c[1] == "pull" and c[2].endswith("cas-boot.img") for c in r.calls))

    def test_patch_skips_inject_when_bake_disabled(self):
        r = FakeRunner()
        with tempfile.TemporaryDirectory() as t:
            stock = pathlib.Path(t) / "stock.img"; stock.write_bytes(b"x")
            cfg = pathlib.Path(t) / "cas-config.json"
            cfg.write_text('{"bake_boot_grant": false}')
            os.environ["CAS_CONFIG"] = str(cfg)
            try:
                ok = PV.patch_init_boot_on_device(Adb(runner=r), stock,
                                                  pathlib.Path(t) / "patched.img", log=lambda *_: None)
            finally:
                del os.environ["CAS_CONFIG"]
        self.assertTrue(ok)
        cmds = "\n".join(r.cmds())
        self.assertNotIn("cas-grant.sh", cmds)             # nothing injected
        self.assertTrue(any(c[1] == "pull" and c[2].endswith("new-boot.img") for c in r.calls))


class FbRunner:
    def __init__(self, flash_ok=True):
        self.calls = []
        self.flash_ok = flash_ok

    def __call__(self, args, input_text=None, timeout=900):
        self.calls.append(list(args))
        if args[-1] == "devices":
            return 0, "ABC123 fastboot\n", ""
        if "flash" in args:
            return (0, "OKAY\n", "") if self.flash_ok else (1, "", "flash failed")
        return 0, "OKAY\n", ""

    def cmds(self):
        return [" ".join(c) for c in self.calls]


class TestSeal(unittest.TestCase):
    def test_seal_sequence(self):
        ra, fb = FakeRunner(), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertTrue(ok)
        a = "\n".join(ra.cmds())
        self.assertIn("development_settings_enabled 0", a)        # dev mode off
        self.assertIn("pm uninstall com.topjohnwu.magisk", a)     # Magisk removed
        self.assertIn("oem_unlock_allowed 0", a)                  # OEM-unlock toggle back off (Root's inverse)
        self.assertIn("adb_enabled 0", a)                         # USB debugging off (last)
        self.assertIn("flash init_boot_a", "\n".join(fb.cmds()))  # un-rooted via stock init_boot
        # USB-debugging-off must be the LAST adb command issued
        self.assertIn("adb_enabled 0", ra.cmds()[-1])

    def test_seal_flashes_detected_active_slot(self):
        # Un-root must flash STOCK to the ACTIVE slot — init_boot_b on a slot-B unit, not a hardcoded _a.
        ra, fb = FakeRunner(slot="_b"), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertTrue(ok)
        self.assertIn("flash init_boot_b", "\n".join(fb.cmds()))
        self.assertNotIn("flash init_boot_a", "\n".join(fb.cmds()))

    def test_seal_hides_dev_options_even_without_root(self):
        # Regression: Developer Options must be hidden even when is_root() is False (a flaky su grant
        # used to skip the dev-options disable, leaving it visible — user-reported). It now runs via the
        # shell uid at the final lockdown step, unconditionally.
        ra, fb = FakeRunner(root=False), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertTrue(ok)
        self.assertIn("development_settings_enabled 0", "\n".join(ra.cmds()))  # hidden despite no root
        self.assertIn("adb_enabled 0", ra.cmds()[-1])                          # USB debugging still last

    def test_seal_warns_upfront_when_not_rooted(self):
        # Option (b): a not-rooted unit gets a clear EARLY warning (may already be sealed / Root skipped),
        # but seal STILL proceeds — the flash-to-guarantee-un-root safety is preserved (not a hard-fail).
        ra, fb = FakeRunner(root=False), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        msgs = []
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=msgs.append, wait=False)
        finally:
            os.unlink(stock)
        self.assertTrue(ok)                                                    # still seals
        # UPFRONT + INFORMATIVE: right after the "SEAL: …" banner, a clear heads-up that the unit reports
        # not-rooted (may already be sealed / Root skipped) and that seal will flash anyway — so the
        # operator realizes immediately rather than after the ~3-min flash.
        head = " ".join(msgs[:2]).lower()
        self.assertIn("not rooted", head)
        self.assertIn("already", head)         # "may already be sealed" context
        self.assertIn("flash", head)           # "flashing anyway to guarantee un-root"

    def test_seal_refuses_golden(self):
        ra, fb = FakeRunner(root=True, golden=True), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertFalse(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))   # golden master is NEVER un-rooted/sealed

    def test_seal_aborts_if_stock_missing(self):
        ok = PV.seal(Adb(runner=FakeRunner()), Fastboot(runner=FbRunner()),
                     "/nonexistent/init_boot.img", log=lambda m: None, wait=False)
        self.assertFalse(ok)

    def test_seal_refuses_model_mismatch(self):
        ra, fb = FakeRunner(model="Odin2 Mini"), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock,
                         log=lambda m: None, wait=False, model_match="Mangmi|Air ?X")
        finally:
            os.unlink(stock)
        self.assertFalse(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))   # never flashed the wrong-model image

    def test_seal_no_strand_on_flash_fail(self):
        ra, fb = FakeRunner(), FbRunner(flash_ok=False)
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertFalse(ok)                              # failed flash -> not sealed
        self.assertIn("reboot", "\n".join(fb.cmds()))     # rebooted out of fastboot (not stranded)
        self.assertNotIn("adb_enabled 0", "\n".join(ra.cmds()))  # USB debugging NOT disabled


class TestRoot(unittest.TestCase):
    def _pc_assets(self):
        """A Magisk-patched init_boot + a Magisk apk sitting on the PC."""
        pf = tempfile.NamedTemporaryFile(suffix=".img", delete=False); pf.write(b"x"); pf.close()
        af = tempfile.NamedTemporaryFile(suffix=".apk", delete=False); af.write(b"x"); af.close()
        return pf.name, af.name

    def test_root_flashes_patched_and_installs_magisk_from_pc(self):
        ra, fb = FakeRunner(root=False), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertTrue(ok)
        self.assertIn("flash init_boot_a", "\n".join(fb.cmds()))     # flashed the PATCHED init_boot
        self.assertIn("reboot fastboot", "\n".join(ra.cmds()))       # entered fastbootd (userspace fastboot) first
        a = "\n".join(ra.cmds())
        self.assertIn("install", a)                                  # installed the Magisk app...
        self.assertIn(apk, a)                                        # ...from the PC apk path (not the SD)

    def test_root_installs_magisk_before_patching(self):
        # Magisk-first: the app install must happen BEFORE the on-device init_boot patch (and the flash).
        ra, fb = FakeRunner(root=False), FbRunner()
        stock, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), stock, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(stock); os.unlink(apk)
        self.assertTrue(ok)
        cmds = ra.cmds()
        install_i = next(i for i, c in enumerate(cmds) if "install" in c)
        patch_i = next(i for i, c in enumerate(cmds) if "boot_patch.sh" in c)
        self.assertLess(install_i, patch_i)                         # Magisk app installed FIRST
        self.assertIn("flash init_boot_a", "\n".join(fb.cmds()))    # then flashed the on-device-patched img

    def test_root_refuses_when_stock_missing(self):
        # No stock init_boot on the PC -> can't patch -> refuse, and never flash.
        ra, fb = FakeRunner(root=False), FbRunner()
        _, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), "/nonexistent/stock.img",
                         magisk_apk=apk, log=lambda m: None, wait=False)
        finally:
            os.unlink(apk)
        self.assertFalse(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))

    def test_root_flashes_detected_active_slot(self):
        # On a unit booted to slot B, root MUST flash init_boot_b — a hardcoded _a would patch the IDLE
        # slot and leave the unit unrooted. The target is detected from the device before fastboot.
        ra, fb = FakeRunner(root=False, slot="_b"), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertTrue(ok)
        self.assertIn("flash init_boot_b", "\n".join(fb.cmds()))
        self.assertNotIn("flash init_boot_a", "\n".join(fb.cmds()))

    def test_root_refuses_model_mismatch(self):
        ra, fb = FakeRunner(model="Retroid Pocket 6", root=False), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False, model_match="Odin2 ?Mini")
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertFalse(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))              # never flashed the wrong-model image

    def test_root_force_proceeds_on_mismatch(self):
        ra, fb = FakeRunner(model="Retroid Pocket 6", root=False), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False, model_match="Odin2 ?Mini", force=True)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertTrue(ok)
        self.assertIn("flash init_boot_a", "\n".join(fb.cmds()))     # FORCED past mismatch -> still flashed

    def test_root_no_strand_on_flash_fail(self):
        ra, fb = FakeRunner(root=False), FbRunner(flash_ok=False)
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertFalse(ok)
        self.assertIn("reboot", "\n".join(fb.cmds()))                # rebooted out of fastboot (not stranded)

    def test_root_refuses_golden(self):
        ra, fb = FakeRunner(root=True, golden=True), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertFalse(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))              # golden is never re-flashed

    def test_root_skips_flash_if_already_rooted(self):
        ra, fb = FakeRunner(root=True, golden=False), FbRunner()
        patched, apk = self._pc_assets()
        try:
            ok = PV.root(Adb(runner=ra), Fastboot(runner=fb), patched, magisk_apk=apk,
                         log=lambda m: None, wait=False)
        finally:
            os.unlink(patched); os.unlink(apk)
        self.assertTrue(ok)
        self.assertNotIn("flash", "\n".join(fb.cmds()))              # already rooted -> no re-flash
        self.assertNotIn("install", "\n".join(ra.cmds()))            # and NO Magisk re-install (fast no-op)


class TestBatch(unittest.TestCase):
    def setUp(self):
        # CAS_CONFIG isolation: test_provision_all_uses_selected_profile_over_model drives provision_all()
        # to a real success (record_download write) with no isolation of its own — see Finding 2.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    def _profile_with_imgs(self, t, name="odin2mini", model="Odin2 ?Mini"):
        prof = make_profile(t, name, model)
        meta = pathlib.Path(t) / name / "profile.meta"
        meta.write_text(meta.read_text() + "patched_init_boot=p.img\nmagisk_apk=m.apk\n")
        (pathlib.Path(t) / "p.img").write_bytes(b"x")
        (pathlib.Path(t) / "m.apk").write_bytes(b"x")
        sf = pathlib.Path(t) / "provision" / "root" / "firmware" / "odin2_20231201" / "init_boot.img"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_bytes(b"x")
        return prof

    def test_root_all_auto_matches_and_skips_unauthorized(self):
        with tempfile.TemporaryDirectory() as t:
            self._profile_with_imgs(t)
            res = PV.root_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Odin2 Mini", root=True)),
                lambda s: Fastboot(serial=s, runner=FbRunner()),
                [("ABC123", "device"), ("DEF456", "unauthorized")],
                profiles_root=t, appdir=t, log=lambda m: None)
            self.assertEqual(res["ABC123"], ("ok", "odin2mini"))    # matched -> rooted (already-rooted path)
            self.assertEqual(res["DEF456"][0], "skip")              # unauthorized skipped

    def test_root_all_uses_default_images_when_profile_unset(self):
        # A profile with NO stock_init_boot/magisk_apk must NOT skip — it falls back to the bundled default
        # kit, so ⓪ Root works fleet-wide with no per-profile picking.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, "p", "Odin2 ?Mini")
            (prof.path / "profile.meta").write_text("model_match=Odin2 ?Mini\n")   # no root images set
            res = PV.root_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Odin2 Mini", root=True)),
                lambda s: Fastboot(serial=s, runner=FbRunner()),
                [("ABC123", "device")], profiles_root=t, appdir=t, log=lambda m: None)
            self.assertEqual(res["ABC123"][0], "ok")        # used the default kit — did NOT skip 'no-init_boot'

    def test_default_root_images_exist(self):
        # Guard against a rename/move breaking ⓪ Root's default-kit fallback. The kit itself is operator-supplied
        # and gitignored (the firmware .img dir + the Apps/ Magisk APK are NOT source), so it is absent on CI /
        # a clean checkout — skip there, and let this actively guard the canonical layout on any bench that has it.
        from cas import APPDIR
        kit = pathlib.Path(APPDIR)
        if not (kit / PV.DEFAULT_STOCK_INIT_BOOT).exists():
            self.skipTest("default-kit firmware (operator-supplied, gitignored) not present in this checkout")
        self.assertTrue((kit / PV.DEFAULT_STOCK_INIT_BOOT).exists(), PV.DEFAULT_STOCK_INIT_BOOT)
        self.assertTrue((kit / PV.DEFAULT_MAGISK_APK).exists(), PV.DEFAULT_MAGISK_APK)

    def test_provision_all_uses_selected_profile_over_model(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, "odin2mini", "Odin2 ?Mini")     # matches Odin, NOT Retroid
            # device reports a model that would NOT auto-match — the SELECTED profile must win anyway.
            res = PV.provision_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Retroid Pocket 6", root=True, sd=True)),
                [("ABC123", "device")], root=t, log=lambda m: None, profile=prof)
            self.assertEqual(res["ABC123"], ("ok", "odin2mini"))   # used the selected profile, ignored model

    def test_root_all_parallel_processes_every_device(self):
        with tempfile.TemporaryDirectory() as t:
            self._profile_with_imgs(t)
            # two 'device'-state units, each its OWN runner (already-rooted -> root() early-returns ok);
            # parallel=True (default) must process BOTH.
            res = PV.root_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Odin2 Mini", root=True)),
                lambda s: Fastboot(serial=s, runner=FbRunner()),
                [("DEV1", "device"), ("DEV2", "device")], profiles_root=t, appdir=t, log=lambda m: None)
            self.assertEqual(set(res), {"DEV1", "DEV2"})            # both handled in parallel
            self.assertTrue(all(v[0] == "ok" for v in res.values()))

    def test_seal_all_auto_matches_and_attempts_flash(self):
        with tempfile.TemporaryDirectory() as t:
            self._profile_with_imgs(t)
            fbs = {}

            def mkfb(s):
                fbs[s] = FbRunner()
                return Fastboot(serial=s, runner=fbs[s])
            PV.seal_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Odin2 Mini", root=True)),
                mkfb, [("ABC123", "device")], profiles_root=t, appdir=t, log=lambda m: None)
            self.assertIn("flash init_boot_a", "\n".join(fbs["ABC123"].cmds()))  # matched -> stock flash issued

    def test_seal_all_uses_default_init_boot_when_profile_unset(self):
        # A profile with NO stock_init_boot must NOT skip Lock — it falls back to the bundled default kit
        # (mirroring Root), so ③ Lock un-roots fleet-wide with no per-profile picking. Regression guard for
        # the root_all/seal_all asymmetry that skipped 'no-init_boot' on default-kit profiles.
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, "p", "Odin2 ?Mini")
            (prof.path / "profile.meta").write_text("model_match=Odin2 ?Mini\n")   # no root images set
            # Stage a stub at DEFAULT_STOCK_INIT_BOOT under a temp appdir so the default-kit fallback resolves
            # HERMETICALLY — the real kit is operator-supplied + gitignored, so it is absent on CI / clean checkouts.
            sf = pathlib.Path(t) / PV.DEFAULT_STOCK_INIT_BOOT
            sf.parent.mkdir(parents=True, exist_ok=True)
            sf.write_bytes(b"x")
            fbs = {}

            def mkfb(s):
                fbs[s] = FbRunner()
                return Fastboot(serial=s, runner=fbs[s])
            res = PV.seal_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Odin2 Mini", root=True)),
                mkfb, [("ABC123", "device")], profiles_root=t, appdir=t, log=lambda m: None)
            self.assertNotEqual(res["ABC123"][0], "no-init_boot")               # did NOT skip
            self.assertIn("flash init_boot", "\n".join(fbs["ABC123"].cmds()))   # used default kit -> stock flash

    def test_root_all_profile_map_routes_per_device_and_skips_none(self):
        # profile_map overrides auto-match: a device whose MODEL matches nothing still gets the mapped
        # profile; a serial mapped to None is skipped (not auto-matched).
        with tempfile.TemporaryDirectory() as t:
            from cas import profiles as P
            self._profile_with_imgs(t, name="odin2mini", model="Odin2 ?Mini")
            pm = {"DEV1": P.Profile(pathlib.Path(t) / "odin2mini"), "DEV2": None}
            res = PV.root_all(
                lambda s: Adb(serial=s, runner=FakeRunner(model="Nomatch", root=True)),
                lambda s: Fastboot(serial=s, runner=FbRunner()),
                [("DEV1", "device"), ("DEV2", "device")],
                profiles_root=t, appdir=t, log=lambda m: None, profile_map=pm)
            self.assertEqual(res["DEV1"][0], "ok")           # mapped profile used despite model not matching
            self.assertEqual(res["DEV2"][0], "no-profile")   # None in the map -> skipped


class TestEsMedia(unittest.TestCase):
    """The shared ES-DE box-art layer (downloaded_media) pushes from the PC, kept out of the golden."""
    class _MR(FakeRunner):
        def __init__(self, has_media=False, **kw):
            super().__init__(**kw)
            self.has_media = has_media

        def __call__(self, args, **kw):
            if "shell" in args and args[-1].startswith("ls ") and "downloaded_media" in args[-1]:
                return 0, ("downloaded_media\n" if self.has_media else ""), ""
            return super().__call__(args, **kw)

    def _media(self, t):
        m = pathlib.Path(t) / "downloaded_media" / "gba"
        m.mkdir(parents=True)
        (m / "x.png").write_bytes(b"x")
        return pathlib.Path(t) / "downloaded_media"

    def test_pushes_when_present_and_device_empty(self):
        with tempfile.TemporaryDirectory() as t:
            src = self._media(t)
            r = self._MR(has_media=False)
            self.assertTrue(PV.push_es_media(Adb(runner=r), log=lambda m: None, media_src=str(src)))
            # new transfer: pack -> push ONE .tar -> unpack on device (not a per-file push of the src dir)
            self.assertTrue(any("push" in c for c in r.calls))
            self.assertTrue(any("tar -xf" in " ".join(c) for c in r.calls))

    def test_skips_when_device_already_has_media(self):
        with tempfile.TemporaryDirectory() as t:
            src = self._media(t)
            r = self._MR(has_media=True)
            self.assertTrue(PV.push_es_media(Adb(runner=r), log=lambda m: None, media_src=str(src)))
            self.assertFalse(any("push" in c for c in r.calls))     # already there -> no 12 GB re-push

    def test_noop_when_no_source(self):
        with tempfile.TemporaryDirectory() as t:
            r = self._MR()
            self.assertFalse(PV.push_es_media(Adb(runner=r), log=lambda m: None,
                                              media_src=str(pathlib.Path(t) / "nope")))
            self.assertFalse(any("push" in c for c in r.calls))


class TestCompanionInstall(unittest.TestCase):
    """The GameCove Companion is a normal golden app; when it's in the manifest the PC-side install
    (adb install) refreshes it to the current PC build after restore. Not in the manifest -> skipped."""

    def setUp(self):
        # CAS_CONFIG isolation: test_provision_skips_companion_when_not_in_manifest runs provision()'s
        # REAL (non-dry) push path to a success with no isolation of its own — see Finding 2.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    def test_installs_from_pc_when_apk_present(self):
        with tempfile.TemporaryDirectory() as t:
            apk = pathlib.Path(t) / "gamecove-companion.apk"
            apk.write_bytes(b"x")
            r = FakeRunner()
            self.assertTrue(PV.install_companion(Adb(runner=r), log=lambda m: None, apk_src=str(apk)))
            a = "\n".join(r.cmds())
            self.assertIn("install", a)            # adb install ...
            self.assertIn(str(apk), a)             # ...from the PC apk path (never the SD)

    def test_noop_when_apk_absent(self):
        r = FakeRunner()
        self.assertFalse(PV.install_companion(
            Adb(runner=r), log=lambda m: None, apk_src="/no/such/companion.apk"))
        self.assertNotIn("install", "\n".join(r.cmds()))

    def test_provision_installs_companion_when_in_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.es_de.frontend", PV.COMPANION_PKG])  # Companion ticked
            apk = pathlib.Path(t) / "gamecove-companion.apk"
            apk.write_bytes(b"x")
            os.environ["CAS_COMPANION_APK"] = str(apk)
            _saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES")}
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            os.environ["CAS_PROFILES"] = t       # isolate apk_store_dir (else install_companion resolves the REAL store)
            try:
                r = FakeRunner()
                ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)   # full (non-dry) path
            finally:
                os.environ.pop("CAS_COMPANION_APK", None)
                for _k, _v in _saved.items():
                    os.environ.pop(_k, None) if _v is None else os.environ.__setitem__(_k, _v)
            self.assertTrue(ok)
            a = "\n".join(r.cmds())
            self.assertIn("install", a)            # companion installed during provisioning
            self.assertIn(str(apk), a)             # ...from the current PC build

    def test_provision_skips_companion_when_not_in_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t)                 # default apps: Companion NOT ticked / not in manifest
            apk = pathlib.Path(t) / "gamecove-companion.apk"
            apk.write_bytes(b"x")
            os.environ["CAS_COMPANION_APK"] = str(apk)
            try:
                r = FakeRunner()
                ok = PV.provision(Adb(runner=r), prof, log=lambda m: None)
            finally:
                os.environ.pop("CAS_COMPANION_APK", None)
            self.assertTrue(ok)
            self.assertNotIn(str(apk), "\n".join(r.cmds()))        # not in manifest -> NOT installed


class TestUpdater(unittest.TestCase):
    """Self-update check against the public GitHub Release latest.json. Network is injected."""

    def _opener(self, manifest):
        import io, json
        data = json.dumps(manifest).encode()
        return lambda url, timeout=0: io.BytesIO(data)

    def test_is_newer_semver(self):
        from cas import updater as U
        self.assertTrue(U.is_newer("0.2.0", "0.1.0"))
        self.assertTrue(U.is_newer("1.0.0", "0.9.9"))
        self.assertTrue(U.is_newer("v0.2.0", "0.1.0"))   # tolerate a leading v
        self.assertFalse(U.is_newer("0.1.0", "0.1.0"))
        self.assertFalse(U.is_newer("0.1.0", "0.2.0"))

    def test_check_returns_update_for_this_os_when_newer(self):
        from cas import updater as U
        man = {"version": "0.2.0", "notes": "new",
               "assets": {"windows": {"url": "http://x/cas-windows.zip", "sha256": "win"},
                          "linux": {"url": "http://x/cas-linux.zip", "sha256": "lin"},
                          "macos": {"url": "http://x/cas-macos.zip", "sha256": "mac"}}}
        up = U.check("0.1.0", opener=self._opener(man), os_name="linux")
        self.assertEqual(up["version"], "0.2.0")
        self.assertEqual(up["url"], "http://x/cas-linux.zip")
        self.assertEqual(up["sha256"], "lin")

    def test_check_none_when_not_newer(self):
        from cas import updater as U
        man = {"version": "0.1.0", "assets": {"linux": {"url": "u", "sha256": "s"}}}
        self.assertIsNone(U.check("0.1.0", opener=self._opener(man), os_name="linux"))

    def test_check_none_when_no_asset_for_os(self):
        from cas import updater as U
        man = {"version": "0.2.0", "assets": {"windows": {"url": "u", "sha256": "s"}}}
        self.assertIsNone(U.check("0.1.0", opener=self._opener(man), os_name="linux"))

    def test_check_never_raises_on_network_error(self):
        from cas import updater as U
        def boom(url, timeout=0):
            raise OSError("offline")
        self.assertIsNone(U.check("0.1.0", opener=boom, os_name="linux"))

    # ---- download progress (issue #2: a real progress indicator) ----
    def test_download_and_verify_reports_progress(self):
        """download_and_verify streams and calls progress(done, total) so the GUI can show a real %."""
        import io
        from cas import updater as U
        payload = b"x" * (1 << 17)            # 128 KiB -> two 64 KiB chunks
        class _Resp(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}
            def __enter__(self): return self
            def __exit__(self, *a): self.close()
        seen = []
        dest = os.path.join(tempfile.mkdtemp(), "u.zip")
        out = U.download_and_verify("http://x/u.zip", dest,
                                    opener=lambda url, timeout=0: _Resp(payload),
                                    progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(out, dest)
        self.assertTrue(seen, "progress callback was never called")
        self.assertEqual(seen[-1], (len(payload), len(payload)))   # ends at 100%
        self.assertTrue(all(t == len(payload) for _, t in seen))   # total reported every time

    # ---- stage_and_relaunch must not silently no-op when there is nothing to swap ----
    def test_stage_refuses_when_no_executable_to_swap(self):
        """Source checkout / wrong layout: appdir has no cas-gui[.exe]. The old code launched a helper
        and returned True (silent no-op -> 'updates but stays the same'). It must now fail loudly."""
        from cas import updater as U
        appdir = tempfile.mkdtemp()                       # NO cas-gui here (mimics `python -m cas`)
        zp = os.path.join(tempfile.mkdtemp(), "cas.zip")
        import zipfile
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("cas/cas-gui", "NEW")
        launched = []
        ok = U.stage_and_relaunch(zp, appdir=appdir, log=lambda *a: None,
                                  platform="linux", launch=lambda *a, **k: launched.append(a))
        self.assertFalse(ok, "must not claim success when there is no executable to replace")
        self.assertEqual(launched, [], "must not launch a swap helper when there's nothing to swap")

    # ---- the Windows helper must be robust when run console-less (the real bench bug) ----
    def test_windows_helper_does_not_use_console_dependent_timeout(self):
        """The helper is launched DETACHED from a windowed (console=False) exe, so `timeout` aborts with
        'Input redirection is not supported' and the wait loop misbehaves. Use a console-independent wait."""
        from cas import updater as U
        script = U._write_helper(pathlib.Path(r"C:\app\dist\cas"),
                                 pathlib.Path(r"C:\stage\cas"), platform="win32", log=lambda *a: None)
        text = pathlib.Path(script).read_text()
        self.assertNotIn("timeout ", text, "timeout needs a console; fails when launched detached")
        self.assertIn("ping", text.lower(), "expected a console-independent delay (ping -n)")

    def test_windows_helper_relaunches_gui_and_logs(self):
        """The Windows helper relaunches cas-gui.exe, copies the new bundle, and writes a diagnostic log
        so a failed swap is no longer a silent black box."""
        from cas import updater as U
        script = U._write_helper(pathlib.Path(r"C:\app\dist\cas"),
                                 pathlib.Path(r"C:\stage\cas"), platform="win32", log=lambda *a: None)
        text = pathlib.Path(script).read_text()
        self.assertIn("cas-gui.exe", text)               # relaunches the GUI exe
        self.assertIn("robocopy", text.lower())          # still an overwrite-copy (never purge siblings)
        self.assertIn("cas-update.log", text)            # leaves a breadcrumb we can read on the bench

    # ---- end-to-end swap via the real unix helper (regression guard for the part that works) ----
    @unittest.skipIf(sys.platform.startswith("win"), "unix helper")
    def test_unix_helper_swaps_bundle_and_preserves_siblings(self):
        from cas import updater as U
        import subprocess, time, zipfile
        root = tempfile.mkdtemp()
        appdir = pathlib.Path(root, "appdir"); (appdir / "_internal").mkdir(parents=True)
        (appdir / "cas-gui").write_text("OLD"); (appdir / "_internal" / "V").write_text("0.1.0")
        (appdir / "data" / "profiles").mkdir(parents=True); (appdir / "data" / "profiles" / "keep").write_text("precious")
        zp = pathlib.Path(root, "cas.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("cas/cas-gui", "NEW"); z.writestr("cas/_internal/V", "0.2.1")
        dead = subprocess.Popen(["true"]); dead.wait()
        captured = {}
        orig = os.getpid; os.getpid = lambda: dead.pid
        try:
            ok = U.stage_and_relaunch(str(zp), appdir=str(appdir), log=lambda *a: None, platform="linux",
                                      launch=lambda helper, **k: captured.setdefault("h", helper))
        finally:
            os.getpid = orig
        self.assertTrue(ok)
        subprocess.run(["/bin/sh", str(captured["h"])], timeout=15)   # run helper directly (pid already dead)
        self.assertEqual((appdir / "_internal" / "V").read_text(), "0.2.1")   # swap took
        self.assertEqual((appdir / "cas-gui").read_text(), "NEW")
        self.assertEqual((appdir / "data" / "profiles" / "keep").read_text(), "precious")  # sibling untouched


class TestEdl(unittest.TestCase):
    GEOM = {"sector_size": "512", "num_sectors": "16384", "partition": "0",
            "start_sector": "16449552", "start_byte_hex": "0x1f6002000"}

    def _runner(self, sahara_ok=True, fh_ok=True):
        calls = []

        def runner(args, input_text=None, timeout=900):
            calls.append(list(args))
            if args[0].endswith("QSaharaServer"):
                return (0, "Sahara protocol completed\nFile transferred successfully\n", "") if sahara_ok \
                    else (0, "ERROR: Could not connect to /dev/ttyUSB0\n", "")
            if args[0].endswith("fh_loader"):
                return (0, "{All Finished Successfully}\n", "") if fh_ok else (0, "FAILED\n", "")
            return 0, "", ""
        return runner, calls

    def test_rawprogram_xml_has_geometry(self):
        from cas.adb import Edl
        xml = Edl.rawprogram_xml("init_boot_b", "patched.img", self.GEOM)
        self.assertIn('label="init_boot_b"', xml)
        self.assertIn('filename="patched.img"', xml)
        self.assertIn('start_sector="16449552"', xml)
        self.assertIn('physical_partition_number="0"', xml)
        self.assertIn('<power value="reset"', xml)   # reboots the unit out of EDL as fh_loader's final act

    def test_flash_partition_success_targets_real_args(self):
        from cas.adb import Edl
        runner, calls = self._runner()
        with tempfile.TemporaryDirectory() as td:
            img = pathlib.Path(td) / "patched.img"; img.write_bytes(b"x" * 32)
            wd = pathlib.Path(td) / "wd"
            edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/prog.elf", runner=runner)
            # re-acquire step: pin the port so it doesn't poll the real (empty) /dev/ttyUSB* for 30s
            edl.find_port = lambda timeout=60, on_tick=None, pattern="/dev/ttyUSB*": "/dev/ttyUSB0"
            self.assertTrue(edl.flash_partition("/dev/ttyUSB0", "init_boot_b", str(img), self.GEOM, str(wd)))
            xml = (wd / "rawprogram_init_boot_b.xml").read_text()
            self.assertIn('start_sector="16449552"', xml)
            self.assertTrue((wd / "patched.img").exists())                 # image staged for --search_path
            fh = [c for c in calls if c[0].endswith("fh_loader")][0]
            self.assertIn("--port=/dev/ttyUSB0", fh)
            self.assertIn("--memoryname=eMMC", fh)

    def test_flash_partition_reacquires_firehose_port_after_sahara(self):
        # Regression (MANGMI AIR X bench): loading the Firehose programmer re-enumerates the unit on USB, so
        # the port can COME BACK AS A DIFFERENT COM. fh_loader must target the RE-ACQUIRED port, not the one
        # Sahara used - else it opens a dead handle and every ReadFile fails ("device is probably not on
        # this port"). Sahara runs on COM3; find_port then reports COM4; fh_loader must use COM4.
        from cas.adb import Edl
        runner, calls = self._runner()
        with tempfile.TemporaryDirectory() as td:
            img = pathlib.Path(td) / "patched.img"; img.write_bytes(b"x" * 32)
            wd = pathlib.Path(td) / "wd"
            edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/prog.elf", runner=runner)
            edl.find_port = lambda timeout=60, on_tick=None, pattern="/dev/ttyUSB*": r"\\.\COM4"
            self.assertTrue(edl.flash_partition(r"\\.\COM3", "init_boot_a", str(img), self.GEOM, str(wd)))
            sahara = [c for c in calls if c[0].endswith("QSaharaServer")][0]
            fh = [c for c in calls if c[0].endswith("fh_loader")][0]
            self.assertIn("-p", sahara); self.assertIn(r"\\.\COM3", sahara)   # Sahara used the original port
            self.assertIn(r"--port=\\.\COM4", fh)                             # fh_loader used the re-acquired one

    def test_flash_partition_succeeds_on_full_transfer_without_finished_banner(self):
        # The MANGMI AIR X write hit 100% ("{percent files transferred 100.00%}") but the USB link dropped
        # as the unit re-enumerated, before fh_loader could print "All Finished Successfully" (and it exited
        # nonzero). The partition IS written, so this must count as success, not a false failure.
        from cas.adb import Edl
        def runner(args, input_text=None, timeout=900):
            if args[0].endswith("QSaharaServer"):
                return 0, "Sahara protocol completed\n", ""
            if args[0].endswith("fh_loader"):
                return 1, "{percent files transferred 100.00%}\n", ""   # 100%, but no banner + nonzero rc
            return 0, "", ""
        with tempfile.TemporaryDirectory() as td:
            img = pathlib.Path(td) / "p.img"; img.write_bytes(b"x")
            edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/prog.elf", runner=runner)
            edl.find_port = lambda timeout=60, on_tick=None, pattern="/dev/ttyUSB*": "/dev/ttyUSB0"
            self.assertTrue(edl.flash_partition("/dev/ttyUSB0", "init_boot_a", str(img), self.GEOM, str(td)))

    def test_flash_partition_fails_when_sahara_cannot_connect(self):
        from cas.adb import Edl
        runner, _ = self._runner(sahara_ok=False)
        with tempfile.TemporaryDirectory() as td:
            img = pathlib.Path(td) / "p.img"; img.write_bytes(b"x")
            edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/prog.elf", runner=runner)
            self.assertFalse(edl.flash_partition("/dev/ttyUSB0", "init_boot_b", str(img), self.GEOM, td))

    def test_reset_reloads_programmer_via_sahara_when_still_stuck_in_edl(self):
        # ③ Lock MANGMI AIR X: a small stock write re-enumerates before the in-rawprogram <power> reset
        # fires, leaving the unit in EDL with the programmer UNLOADED (Sahara mode). A bare `fh_loader
        # --reset` fails WITHOUT the port dying; reset() must RE-LOAD the programmer via Sahara on a retry,
        # then reset. (The failure text must NOT look like a dead port, or it'd be read as already-rebooted.)
        from cas.adb import Edl
        calls = []
        def runner(args, input_text=None, timeout=900, cwd=None):
            calls.append(list(args))
            if args[0].endswith("QSaharaServer"):
                return 0, "Sahara protocol completed\n", ""
            if args[0].endswith("fh_loader"):     # --reset succeeds ONLY once the programmer was re-loaded
                if any(c[0].endswith("QSaharaServer") for c in calls):
                    return 0, "{All Finished Successfully}\n", ""
                return 1, "ERROR: Firehose target sent nak / no response\n", ""   # present, not a dead port
            return 0, "", ""
        with tempfile.TemporaryDirectory() as td:
            edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/prog.elf", runner=runner)
            edl.find_port = lambda timeout=10, on_tick=None, pattern="/dev/ttyUSB*": "/dev/ttyUSB0"  # still in EDL
            with patch("cas.adb.time.sleep", lambda *_a, **_k: None):
                self.assertTrue(edl.reset("/dev/ttyUSB0", td, log=lambda *_: None))
            self.assertTrue(any(c[0].endswith("QSaharaServer") for c in calls),
                            "reset must re-load the Firehose programmer via Sahara when the unit is stuck")

    def test_reset_stops_quietly_when_unit_already_left_edl(self):
        # ③ Lock on Arch/Linux: the <power> reset already rebooted the unit, so a bare --reset hits a DEAD
        # port ("could not read ... not on this port"). reset() must read that as rebooted and STOP at the
        # first attempt — not retry 3x, spewing fh_loader errors + port_trace.txt writes into the dead port.
        from cas.adb import Edl
        calls = []
        def runner(args, input_text=None, timeout=900, cwd=None):
            calls.append(list(args))
            if args[0].endswith("fh_loader"):
                return 1, "ERROR: Could not read from '/dev/ttyUSB0', device is probably *not* on this port\n", ""
            return 0, "", ""
        with tempfile.TemporaryDirectory() as td:
            edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/prog.elf", runner=runner)
            edl.find_port = lambda timeout=10, on_tick=None, pattern="/dev/ttyUSB*": "/dev/ttyUSB0"  # stale node
            with patch("cas.adb.time.sleep", lambda *_a, **_k: None):
                self.assertTrue(edl.reset("/dev/ttyUSB0", td, log=lambda *_: None))   # dead port = rebooted
            self.assertEqual(sum(1 for c in calls if c[0].endswith("fh_loader")), 1,
                             "one fh_loader call only — stopped instead of retrying into the dead port")
            self.assertFalse(any(c[0].endswith("QSaharaServer") for c in calls),
                             "no Sahara re-load once the unit has already left EDL")

    def test_reset_points_fh_loader_at_the_workdir_for_its_port_trace(self):
        # fh_loader dumps a port_trace.txt into its CWD on error; a prior pkexec run can leave a root-owned
        # one in the app's CWD ("Could not append"). reset() must run the tools with cwd=<workdir>.
        from cas.adb import Edl
        seen = {}
        def runner(args, input_text=None, timeout=900, cwd=None):
            if args[0].endswith("fh_loader"):
                seen["cwd"] = cwd
            return 1, "could not read from port\n", ""
        with tempfile.TemporaryDirectory() as td:
            edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/prog.elf", runner=runner)
            edl.find_port = lambda timeout=10, on_tick=None, pattern="/dev/ttyUSB*": "/dev/ttyUSB0"
            with patch("cas.adb.time.sleep", lambda *_a, **_k: None):
                edl.reset("/dev/ttyUSB0", td, log=lambda *_: None)
            self.assertEqual(seen.get("cwd"), td)      # trace goes to the temp dir, not the repo/app CWD

    @unittest.skipUnless(os.name == "posix", "staging (copy + chmod) is POSIX-only; Windows runs in place")
    def test_staged_exec_makes_a_local_executable_copy(self):
        # POSIX behavior: NAS/CIFS forces file_mode=0664 (non-exec), so tools are copied local + chmod +x.
        # Skipped on Windows, where the tool runs in place (test_staged_exec_runs_in_place_on_windows) - and
        # where forcing os.name="posix" would make pathlib try to build a PosixPath and crash.
        from cas.adb import Edl
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "QSaharaServer"; src.write_text("#!/bin/sh\n"); src.chmod(0o644)
            wd = pathlib.Path(td) / "wd"; wd.mkdir()
            out = Edl(str(src), "/x/fh_loader", "/x/p.elf")._staged_exec(str(src), wd)
            self.assertEqual(pathlib.Path(out).parent, wd)        # staged into the local workdir
            self.assertTrue(os.access(out, os.X_OK))              # and now executable

    def test_staged_exec_runs_in_place_on_windows(self):
        # Windows has no noexec mount, and copying a fresh flashing .exe into %TEMP% only invites Defender
        # to scan/block it (the MANGMI "Sahara couldn't open the port" bench). So on Windows the tool runs
        # IN PLACE from the payload - the exact path proven to work by hand - never copied into workdir.
        from cas import adb as A
        from cas.adb import Edl
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "QSaharaServer.exe"; src.write_bytes(b"MZfake")
            wd = pathlib.Path(td) / "wd"; wd.mkdir()
            with mock.patch.object(A.os, "name", "nt"):
                out = Edl(str(src), "/x/fh_loader.exe", "/x/p.elf")._staged_exec(str(src), wd)
            self.assertEqual(out, str(src))                       # unchanged - run in place
            self.assertFalse((wd / "QSaharaServer.exe").exists()) # nothing copied into %TEMP%

    def test_launcher_noop_with_mocked_runner(self):
        # Auto-escalation must never fire under a mocked runner (tests / non-real flows), whatever the port.
        from cas.adb import Edl
        runner, _ = self._runner()
        self.assertEqual(Edl("/x/q", "/x/f", "/x/p", runner=runner)._launcher("/dev/ttyUSB0"), [])

    def test_launcher_noop_when_port_absent_or_empty(self):
        # Real runner but no such port node (also covers root / non-POSIX) -> escalate nothing.
        from cas.adb import Edl, subprocess_runner
        edl = Edl("/x/q", "/x/f", "/x/p", runner=subprocess_runner)
        self.assertEqual(edl._launcher("/dev/cas-nonexistent-port"), [])
        self.assertEqual(edl._launcher(""), [])

    def test_edl_ports_posix_globs_ttyusb(self):
        from cas import adb as A
        from unittest import mock
        with mock.patch.object(A.os, "name", "posix"), \
             mock.patch.object(A.glob, "glob", return_value=["/dev/ttyUSB1", "/dev/ttyUSB0"]):
            self.assertEqual(A._edl_ports("/dev/ttyUSB*"), ["/dev/ttyUSB0", "/dev/ttyUSB1"])   # sorted

    def test_edl_ports_windows_formats_com_port(self):
        # Windows has no /dev glob: the QDLoader 9008 COM port(s) are returned as \\.\COMn (the form the
        # EDL tools want; the \\.\ prefix also handles COM10+). Regression for EDL on a Windows bench.
        from cas import adb as A
        from unittest import mock
        with mock.patch.object(A.os, "name", "nt"), \
             mock.patch.object(A, "_windows_edl_com_ports", return_value=["COM7", "COM12"]):
            self.assertEqual(A._edl_ports(), [r"\\.\COM7", r"\\.\COM12"])

    def test_find_port_returns_first_available_com(self):
        from cas.adb import Edl
        from cas import adb as A
        from unittest import mock
        edl = Edl("/x/q", "/x/f", "/x/p")
        # _edl_ports already returns only PRESENT ports (Windows filters by SERIALCOMM), so a name here
        # means the device is really there - find_port returns it.
        with mock.patch.object(A, "_edl_ports", return_value=[r"\\.\COM3"]):
            self.assertEqual(edl.find_port(timeout=2), r"\\.\COM3")

    def test_find_port_waits_until_the_port_is_present(self):
        # Regression (MANGMI AIR X bench): on Windows the registry keeps a 9008 device's PortName after
        # unplug, so a naive scan 'saw' COM3 the instant we looked - while the unit was still rebooting into
        # EDL - and find_port returned a dead port that QSaharaServer couldn't open. _edl_ports now filters
        # to PRESENT ports (SERIALCOMM), so it returns [] until the unit re-enumerates. find_port must wait
        # out those empty polls and return the port only once it actually shows up.
        from cas.adb import Edl
        from cas import adb as A
        from unittest import mock
        edl = Edl("/x/q", "/x/f", "/x/p")
        ports = mock.Mock(side_effect=[[], [], [r"\\.\COM3"]])   # absent, absent, then present
        with mock.patch.object(A, "_edl_ports", ports), \
             mock.patch.object(A.time, "sleep", lambda *_a, **_k: None):
            self.assertEqual(edl.find_port(timeout=30), r"\\.\COM3")
        self.assertEqual(ports.call_count, 3)   # waited out two empty polls, returned on the third

    def test_find_port_none_on_timeout(self):
        from cas.adb import Edl
        from cas import adb as A
        from unittest import mock
        edl = Edl("/x/q", "/x/f", "/x/p")
        with mock.patch.object(A, "_edl_ports", return_value=[]), \
             mock.patch.object(A.time, "sleep", lambda *_a, **_k: None):
            self.assertIsNone(edl.find_port(timeout=2))

    def test_windows_edl_com_ports_never_raises(self):
        # Off Windows winreg is absent -> [] (no crash). On Windows with no 9008 attached -> also []. The
        # invariant we lock in: it always returns a list and never raises, so find_port stays safe anywhere.
        from cas import adb as A
        self.assertIsInstance(A._windows_edl_com_ports(), list)
        self.assertIsInstance(A._windows_active_com_ports(), set)   # its presence filter, also crash-proof


class TestFlashers(unittest.TestCase):
    def test_fastboot_flasher_success_and_failure(self):
        from cas.adb import Adb, Fastboot
        from cas import provision as PV

        def fb_ok(args, input_text=None, timeout=900):
            return (0, "SER\t fastboot\n", "") if args[-1] == "devices" else (0, "", "")

        def fb_flashfail(args, input_text=None, timeout=900):
            if args[-1] == "devices":
                return 0, "SER\t fastboot\n", ""
            return (1, "", "FAILED (remote: 'unknown command')") if "flash" in args else (0, "", "")
        adb = Adb(runner=FakeRunner())
        self.assertTrue(PV.fastboot_flasher(Fastboot(serial="SER", runner=fb_ok))(
            adb, "init_boot_a", "/tmp/p.img", lambda *a: None))
        self.assertFalse(PV.fastboot_flasher(Fastboot(serial="SER", runner=fb_flashfail))(
            adb, "init_boot_a", "/tmp/p.img", lambda *a: None))

    @staticmethod
    def _reboot_target(args):
        i = args.index("reboot")
        return args[i + 1] if i + 1 < len(args) else "os"

    def test_fastboot_flasher_prefers_fastbootd(self):
        """The flash enters FASTBOOTD (userspace) first — some unlocked bootloaders reject `flash` but
        fastbootd accepts it (e.g. Retroid). On success it must NOT touch bootloader mode or reflash."""
        from cas.adb import Adb, Fastboot
        from cas import provision as PV
        seq = []

        def adb_runner(args, input_text=None, timeout=900):
            if "reboot" in args:
                seq.append("adb:reboot:" + self._reboot_target(args))
            return (0, "", "")

        def fb_runner(args, input_text=None, timeout=900):
            if args[-1] == "devices":
                return (0, "SER\t fastboot\n", "")
            if "flash" in args:
                seq.append("fb:flash")
            elif "reboot" in args:
                seq.append("fb:reboot:" + self._reboot_target(args))
            return (0, "", "")
        ok = PV.fastboot_flasher(Fastboot(serial="SER", runner=fb_runner))(
            Adb(runner=adb_runner), "init_boot_a", "/tmp/p.img", lambda *a: None)
        self.assertTrue(ok)
        self.assertEqual(seq[0], "adb:reboot:fastboot")     # fastbootd first
        self.assertEqual(seq.count("fb:flash"), 1)          # one flash, no bootloader retry
        self.assertNotIn("adb:reboot:bootloader", seq)
        self.assertNotIn("fb:reboot:bootloader", seq)

    def test_fastboot_flasher_falls_back_to_bootloader_on_fastbootd_reject(self):
        """If fastbootd rejects the flash, hop to bootloader fastboot (via the fastboot-side reboot, since a
        fastboot device is present) and retry there."""
        from cas.adb import Adb, Fastboot
        from cas import provision as PV
        seq = []
        state = {"flashes": 0}

        def adb_runner(args, input_text=None, timeout=900):
            if "reboot" in args:
                seq.append("adb:reboot:" + self._reboot_target(args))
            return (0, "", "")

        def fb_runner(args, input_text=None, timeout=900):
            if args[-1] == "devices":
                return (0, "SER\t fastboot\n", "")
            if "flash" in args:
                state["flashes"] += 1
                seq.append("fb:flash")
                # reject in fastbootd (1st), accept in bootloader (2nd)
                return (1, "", "FAILED (remote: 'not allowed')") if state["flashes"] == 1 else (0, "", "")
            if "reboot" in args:
                seq.append("fb:reboot:" + self._reboot_target(args))
            return (0, "", "")
        ok = PV.fastboot_flasher(Fastboot(serial="SER", runner=fb_runner))(
            Adb(runner=adb_runner), "init_boot_a", "/tmp/p.img", lambda *a: None)
        self.assertTrue(ok)
        self.assertEqual(seq[0], "adb:reboot:fastboot")     # fastbootd first
        self.assertIn("fb:reboot:bootloader", seq)          # in-fastboot -> fastboot-side hop to bootloader
        self.assertEqual(seq.count("fb:flash"), 2)          # failed in fastbootd, succeeded in bootloader

    def test_fastboot_flasher_falls_back_when_no_fastbootd(self):
        """A unit with no userspace fastboot never appears after `reboot fastboot`; the flasher reboots to the
        bootloader via ADB (it's still in the OS) and flashes there."""
        from cas.adb import Adb, Fastboot
        from cas import provision as PV
        seq = []

        def adb_runner(args, input_text=None, timeout=900):
            if "reboot" in args:
                seq.append("adb:reboot:" + self._reboot_target(args))
            return (0, "", "")

        def fb_runner(args, input_text=None, timeout=900):
            if args[-1] == "devices":
                return (0, "SER\t fastboot\n", "")
            if "flash" in args:
                seq.append("fb:flash")
            elif "reboot" in args:
                seq.append("fb:reboot:" + self._reboot_target(args))
            return (0, "", "")
        fb = Fastboot(serial="SER", runner=fb_runner)
        waits = iter([False, True])                          # fastbootd wait fails; bootloader wait succeeds
        fb.wait = lambda on_tick=None, timeout=60: next(waits)
        ok = PV.fastboot_flasher(fb)(Adb(runner=adb_runner), "init_boot_a", "/tmp/p.img", lambda *a: None)
        self.assertTrue(ok)
        self.assertEqual(seq[0], "adb:reboot:fastboot")     # tried fastbootd
        self.assertIn("adb:reboot:bootloader", seq)         # no fastbootd -> adb reboot bootloader (in the OS)
        self.assertNotIn("fb:reboot:bootloader", seq)       # did NOT use the fastboot-side hop
        self.assertEqual(seq.count("fb:flash"), 1)

    def test_edl_flasher_success(self):
        from cas.adb import Adb, Edl
        from cas import provision as PV

        def runner(args, input_text=None, timeout=900, cwd=None):
            if args[0].endswith("QSaharaServer"):
                return 0, "Sahara protocol completed\n", ""
            if args[0].endswith("fh_loader"):
                return 0, "{All Finished Successfully}\n", ""
            return 0, "", ""
        edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/p.elf", runner=runner)
        edl.find_port = lambda timeout=60, on_tick=None, pattern="/dev/ttyUSB*": "/dev/ttyUSB0"
        geom = {"sector_size": "512", "num_sectors": "1", "partition": "0",
                "start_sector": "1", "start_byte_hex": "0x0"}
        flasher = PV.edl_flasher(edl, geom)
        with tempfile.TemporaryDirectory() as td:
            img = pathlib.Path(td) / "p.img"; img.write_bytes(b"x")
            self.assertTrue(flasher(Adb(runner=FakeRunner()), "init_boot_b", str(img), lambda *a: None))

    def test_edl_sahara_failure_logs_tool_output_and_exit_code(self):
        # Regression (MANGMI AIR X bench): the cancel-aware subprocess_runner merges stderr into stdout and
        # returns err="", but the failure branches logged only `err` -> empty. CAS then printed a hardcoded
        # GUESS ("install the QDLoader 9008 driver") on a bench whose driver + COM3 were provably fine.
        # The log must instead carry what the tool actually said, plus its exit code.
        from cas.adb import Adb, Edl
        from cas import adb as A
        from cas import provision as PV
        from unittest import mock

        def runner(args, input_text=None, timeout=900, cwd=None):
            if args[0].endswith("QSaharaServer"):
                return 3, "Sahara: Cannot open port \\\\.\\COM3 (access denied)\n", ""   # err EMPTY on purpose
            return 0, "", ""
        edl = Edl("/x/QSaharaServer", "/x/fh_loader", "/x/p.elf", runner=runner)
        edl.find_port = lambda timeout=60, on_tick=None, pattern="/dev/ttyUSB*": "/dev/ttyUSB0"
        geom = {"sector_size": "512", "num_sectors": "1", "partition": "0",
                "start_sector": "1", "start_byte_hex": "0x0"}
        lines = []
        flasher = PV.edl_flasher(edl, geom)
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(A.time, "sleep", lambda *_a, **_k: None):   # don't wait out reset() retries
            img = pathlib.Path(td) / "p.img"; img.write_bytes(b"x")
            self.assertFalse(flasher(Adb(runner=FakeRunner()), "init_boot_b", str(img), lines.append))
        blob = "\n".join(lines)
        self.assertIn("Cannot open port", blob)          # the tool's REAL message reached the operator
        self.assertIn("exit 3", blob)                     # ...and its exit code
        self.assertNotIn("driver isn't installed", blob)  # ...and we no longer assert a cause we can't know

    def test_root_dispatches_to_provided_flasher(self):
        from cas.adb import Adb, Fastboot
        from cas import provision as PV
        orig = PV.patch_init_boot_on_device
        PV.patch_init_boot_on_device = lambda adb, stock, out, log=print: True   # isolate dispatch
        try:
            seen = {}

            def fake_flasher(adb, target, image, log):
                seen["target"] = target
                return True
            with tempfile.TemporaryDirectory() as td:
                stock = pathlib.Path(td) / "init_boot.img"; stock.write_bytes(b"x")
                ok = PV.root(Adb(runner=FakeRunner(root=False)), Fastboot(runner=lambda *a, **k: (0, "", "")),
                             str(stock), magisk_apk=None, log=lambda *a: None, wait=False, flasher=fake_flasher)
                self.assertTrue(ok)
                self.assertEqual(seen.get("target"), "init_boot_a")    # FakeRunner: first_api 33, slot _a
        finally:
            PV.patch_init_boot_on_device = orig

    def test_seal_dispatches_to_provided_flasher_with_stock_image(self):
        from cas.adb import Adb, Fastboot
        from cas import provision as PV
        seen = {}

        def fake_flasher(adb, target, image, log):
            seen["target"] = target
            seen["image"] = image
            return True
        with tempfile.TemporaryDirectory() as td:
            stock = pathlib.Path(td) / "init_boot.img"; stock.write_bytes(b"x")
            ok = PV.seal(Adb(runner=FakeRunner(root=False)), Fastboot(runner=lambda *a, **k: (0, "", "")),
                         str(stock), log=lambda *a: None, wait=False, flasher=fake_flasher)
            self.assertTrue(ok)
            self.assertEqual(seen.get("target"), "init_boot_a")
            self.assertEqual(seen.get("image"), str(stock))     # seal un-roots by flashing STOCK


class TestCancel(unittest.TestCase):
    def test_subprocess_runner_cancel_kills_and_returns_CANCELLED(self):
        import threading
        from cas.adb import subprocess_runner, CANCELLED, is_cancelled
        ev = threading.Event(); ev.set()                 # pre-set → first poll aborts immediately
        rc, out, err = subprocess_runner([sys.executable, "-c", "import time; time.sleep(30)"], cancel=ev)
        self.assertEqual(rc, CANCELLED)
        self.assertTrue(is_cancelled(rc))

    def test_subprocess_runner_without_cancel_is_unchanged(self):
        from cas.adb import subprocess_runner
        rc, out, _ = subprocess_runner([sys.executable, "-c", "print('hi')"])
        self.assertEqual((rc, out.strip()), (0, "hi"))

    def test_wait_loops_bail_on_cancel(self):
        import threading
        from cas.adb import Adb, Fastboot, Edl
        ev = threading.Event(); ev.set()
        self.assertFalse(Adb(runner=FakeRunner(), cancel=ev).wait_boot(timeout=10))
        self.assertFalse(Fastboot(runner=lambda *a, **k: (0, "", ""), cancel=ev).wait(timeout=10))
        self.assertIsNone(Edl("/x/q", "/x/f", "/x/p", cancel=ev).find_port(timeout=10))

    def test_root_all_worker_reports_cancelled(self):
        import threading
        from cas import provision as PV
        from cas.adb import Adb, Fastboot
        ev = threading.Event(); ev.set()
        res = PV.root_all(lambda s: Adb(serial=s, runner=FakeRunner(), cancel=ev),
                          lambda s: Fastboot(serial=s, runner=lambda *a, **k: (0, "", ""), cancel=ev),
                          [("S", "device")], log=lambda *a: None)
        self.assertEqual(res["S"][0], "cancelled")

    def test_fastboot_flasher_brackets_the_critical_write(self):
        from cas.adb import Adb, Fastboot
        from cas import provision as PV
        events = []

        def fb_ok(args, input_text=None, timeout=900):
            return (0, "SER\t fastboot\n", "") if args[-1] == "devices" else (0, "", "")
        PV.fastboot_flasher(Fastboot(serial="SER", runner=fb_ok), on_critical=events.append)(
            Adb(runner=FakeRunner()), "init_boot_a", "/tmp/p.img", lambda *a: None)
        self.assertEqual(events, [True, False])          # marked entering + leaving the partition write


class TestWarmup(unittest.TestCase):
    """③ Warm up — launch every manifest app once (frontends last); a settle+sweep at the end keeps
    them out of recents; a pass that warms NOTHING fails; never block a seal on a partial miss."""

    def setUp(self):
        # F8: isolate CAS_CONFIG so a bench's real cas-config.json (e.g. a custom warmup_skip_pkgs) can
        # never leak into a case that doesn't pass skip=/dwell=/settle= explicitly and turn it red.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")
        self._tmp_cfg = tempfile.TemporaryDirectory()
        os.environ["CAS_CONFIG"] = str(pathlib.Path(self._tmp_cfg.name) / "cas-config.json")
        # auto_grant_shell defaults ON, and warmup() re-takes a lost shell grant before refusing — so an
        # unrooted case would call the REAL grant_shell_root, which polls uiautomator with 1s sleeps (it
        # made this class take 135s). Pin it OFF; the one test that exercises the re-grant turns it back on.
        from cas import config as C
        C.save_config({"auto_grant_shell": False})

    def tearDown(self):
        self._tmp_cfg.cleanup()
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    class WarmRunner(FakeRunner):
        """FakeRunner that records launches/foreground polls. `absent` = pkgs `pm path` won't resolve;
        `never_fg` = pkgs that launch but never become the resumed activity."""

        def __init__(self, absent=(), never_fg=(), **kw):
            super().__init__(**kw)
            self.absent, self.never_fg = set(absent), set(never_fg)
            self.launched = []          # pkgs launched, in order
            self.fg = ""                # the pkg currently "resumed" on the fake device
            self.fg_timeouts = []       # each `timeout` passed alongside a topResumedActivity dumpsys call

        def __call__(self, args, input_text=None, timeout=900):
            if "shell" in args:
                tail = args[-1]
                if tail.startswith("pm path "):
                    pkg = tail.split()[-1]
                    self.calls.append(list(args))
                    return (1, "", "") if pkg in self.absent else (0, f"package:/data/app/{pkg}.apk\n", "")
                if tail.startswith("monkey -p "):
                    pkg = tail.split()[2]
                    self.launched.append(pkg)
                    self.fg = "" if pkg in self.never_fg else pkg
                    self.calls.append(list(args))
                    return 0, "Events injected: 1\n", ""
                if "topResumedActivity" in tail:
                    self.calls.append(list(args))
                    self.fg_timeouts.append(timeout)
                    return 0, (f"topResumedActivity=ActivityRecord{{u0 {self.fg}/.Main}}\n"
                               if self.fg else "topResumedActivity=null\n"), ""
            return super().__call__(args, input_text=input_text, timeout=timeout)

    def _warm(self, prof, runner, **kw):
        """Run warmup() with a zero dwell/settle by default so tests never actually sleep real seconds;
        a test can override either via kwargs."""
        logs = []
        kw.setdefault("dwell", 0)
        kw.setdefault("settle", 0)
        ok = PV.warmup(Adb(runner=runner), prof, log=logs.append, **kw)
        return ok, logs

    def test_hermetic_config_isolation(self):
        """F8: setUp points CAS_CONFIG at a fresh temp file, so config getters here read DEFAULTS — never
        a bench's real cas-config.json (which could carry a custom skip-list). setUp pins exactly ONE key
        (auto_grant_shell, to keep the real uiautomator re-grant out of the unrooted cases); nothing else
        may leak in, and every warm-up getter must still read its default."""
        from cas import config as C
        self.assertEqual(set(C.load_config()), {"auto_grant_shell"})   # no bench keys leaked in
        self.assertEqual(C.warmup_skip_pkgs(), frozenset({"com.topjohnwu.magisk"}))
        self.assertEqual(C.warmup_dwell_s(), 1.0)
        self.assertEqual(C.warmup_settle_s(), 10.0)

    def test_launches_every_manifest_app_frontends_last(self):
        with tempfile.TemporaryDirectory() as t:
            # ES-DE is in the manifest; com.handheld.launcher is a SYSTEM app (never in a manifest) but
            # is present on the unit — both must warm, and both must come AFTER the emulators.
            prof = make_profile(t, apps=["org.es_de.frontend", "org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner()
            ok, _ = self._warm(prof, r)
            self.assertTrue(ok)
            self.assertEqual(r.launched, ["org.ppsspp.ppsspp", "org.citra.emu",
                                          "org.es_de.frontend", "com.handheld.launcher"])

    def test_no_force_stop_interleaved_during_the_pass(self):
        """A force-stop right after a dwell would kill a scan mid-flight — the bug this step exists to
        fix. The sweep (F3) does force-stop every launched app, but only ONCE at the very end, after the
        settle — never interleaved between individual app launches.

        N7: the original assertion only checked that any force-stops PRESENT came after the last launch —
        vacuously true if the sweep were deleted entirely. Assert the sweep actually happened too, so this
        test is pinned to "no force-stop happens BEFORE the sweep", not "no force-stop happens at all"."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner()
            self._warm(prof, r)
            cmds = r.cmds()
            launch_idxs = [i for i, c in enumerate(cmds) if "monkey -p" in c]
            fs_idxs = [i for i, c in enumerate(cmds) if "force-stop" in c]
            self.assertTrue(launch_idxs)
            self.assertTrue(fs_idxs, "the sweep's force-stops never ran (test would pass vacuously "
                                      "if the sweep were deleted)")
            self.assertTrue(all(i > launch_idxs[-1] for i in fs_idxs))

    def test_sweep_force_stops_every_launched_app_after_settle_then_home(self):
        """F3: the pass ends with settle -> sweep -> home. Every app actually launched gets force-
        stopped, in a block strictly after the last launch, and HOME is the very last call."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner()
            ok, _ = self._warm(prof, r)
            self.assertTrue(ok)
            cmds = r.cmds()
            for pkg in r.launched:
                self.assertTrue(any(f"force-stop {pkg}" in c for c in cmds),
                                f"{pkg} was launched but never swept (force-stop)")
            self.assertIn("android.intent.category.HOME", cmds[-1])

    def test_settle_runs_before_the_sweep(self):
        """F3/F7: the settle uses the cancel-aware sleep helper, and runs strictly before the sweep.

        N7: the original version only checked that the settle duration was the LAST _cancel_sleep call —
        it never actually looked at adb calls, so "before the sweep" was asserted in name only. Record the
        adb call count at the moment the settle-duration sleep fires, then confirm no force-stop appears
        before that point and the sweep's force-stops appear after it."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            r = self.WarmRunner()
            waited = []
            settle_call_idx = []
            def fake_sleep(cancel, seconds):
                waited.append(seconds)
                if seconds == 17:                     # the settle call specifically (dwell defaults to 0)
                    settle_call_idx.append(len(r.calls))
                return True
            with patch.object(PV, "_cancel_sleep", fake_sleep):
                ok, _ = self._warm(prof, r, settle=17)
            self.assertTrue(ok)
            self.assertEqual(waited[-1], 17)          # the settle call is the LAST _cancel_sleep call
            self.assertTrue(settle_call_idx)
            idx = settle_call_idx[0]
            cmds = r.cmds()
            self.assertFalse(any("force-stop" in c for c in cmds[:idx]),
                              "a force-stop happened BEFORE the settle sleep was even invoked")
            self.assertTrue(any("force-stop" in c for c in cmds[idx:]),
                              "the sweep's force-stops never ran after the settle")

    def test_cancel_during_settle_returns_false_and_skips_the_sweep(self):
        """F7: a cancel that lands during the settle must stop the pass — the sweep (and its force-stop
        calls) must never run."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            r = self.WarmRunner()
            def fake_sleep(cancel, seconds):
                return seconds != 999          # the settle call (seconds=999) reports "cancelled"
            with patch.object(PV, "_cancel_sleep", fake_sleep):
                ok, logs = self._warm(prof, r, settle=999)
            self.assertFalse(ok)
            self.assertNotIn("force-stop", "\n".join(r.cmds()))
            self.assertIn("cancel", "\n".join(logs).lower())

    def test_dwell_uses_the_cancel_aware_sleep_helper(self):
        """F7: raising warmup_dwell_s (the design's own tuning lever) must not raise cancel latency by
        the same amount — the per-app dwell goes through the cancel-aware helper too."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            r = self.WarmRunner()
            waited = []
            def fake_sleep(cancel, seconds):
                waited.append(seconds)
                return True
            with patch.object(PV, "_cancel_sleep", fake_sleep):
                self._warm(prof, r, dwell=7, settle=0)
            self.assertIn(7, waited)

    def test_skip_list_pkg_is_never_launched(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["com.topjohnwu.magisk", "org.ppsspp.ppsspp"])
            r = self.WarmRunner()
            self._warm(prof, r, skip=frozenset({"com.topjohnwu.magisk"}))
            self.assertNotIn("com.topjohnwu.magisk", r.launched)
            self.assertIn("org.ppsspp.ppsspp", r.launched)

    def test_absent_pkg_is_skipped_not_launched(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner(absent={"org.citra.emu", "com.handheld.launcher", "org.es_de.frontend"})
            ok, logs = self._warm(prof, r)
            self.assertTrue(ok)                                   # an absent app is a skip, not a failure
            self.assertEqual(r.launched, ["org.ppsspp.ppsspp"])
            self.assertIn("org.citra.emu", "\n".join(logs))       # …and it's reported

    def test_app_that_never_foregrounds_warns_and_continues(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner(never_fg={"org.ppsspp.ppsspp"})
            with patch.object(PV, "WARMUP_FOREGROUND_TIMEOUT", 0.05):
                ok, logs = self._warm(prof, r)
            self.assertTrue(ok)                                   # additive: a miss never blocks the seal
            joined = "\n".join(logs)
            self.assertIn("[warn]", joined)
            self.assertIn("org.ppsspp.ppsspp", joined)
            self.assertIn("org.citra.emu", r.launched)            # the pass carried on to the next app

    def test_substring_collision_does_not_falsely_match_foreground(self):
        """F6: matching a raw substring (old `pkg in foreground(...)`) let org.ppsspp.ppsspp falsely
        match a foreground of org.ppsspp.ppssppgold. The fix matches f'{pkg}/' instead."""
        class CollisionRunner(self.WarmRunner):
            def __call__(self, args, input_text=None, timeout=900):
                if "shell" in args and args[-1].startswith("monkey -p org.ppsspp.ppsspp "):
                    self.calls.append(list(args))
                    self.launched.append("org.ppsspp.ppsspp")
                    self.fg = "org.ppsspp.ppssppgold"    # look-alike package becomes the REAL foreground
                    return 0, "Events injected: 1\n", ""
                return super().__call__(args, input_text=input_text, timeout=timeout)
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            r = CollisionRunner()
            with patch.object(PV, "WARMUP_FOREGROUND_TIMEOUT", 0.05):
                ok, logs = self._warm(prof, r)
            self.assertTrue(ok)                          # additive: a miss must not fail the whole pass
            joined = "\n".join(logs)
            self.assertIn("[warn]", joined)
            self.assertIn("org.ppsspp.ppsspp", joined)    # correctly reported as a MISS, not a false [ok]

    def test_foreground_poll_uses_a_bounded_timeout(self):
        """F4: each dumpsys probe inside the wait loop must be bounded — an unbounded call would inherit
        the runner's 900s default and could blow WARMUP_FOREGROUND_TIMEOUT by 60x on one wedge."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            r = self.WarmRunner()
            self._warm(prof, r)
            self.assertTrue(r.fg_timeouts)
            self.assertTrue(all(tmo == PV.WARMUP_FOREGROUND_POLL_TIMEOUT for tmo in r.fg_timeouts))

    def test_pass_ends_at_home(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.WarmRunner()
            self._warm(make_profile(t, apps=["org.ppsspp.ppsspp"]), r)
            self.assertIn("android.intent.category.HOME", " ".join(r.calls[-1]))

    def test_cancel_stops_the_pass_between_apps(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner()
            ev = threading.Event()
            ev.set()                                              # already cancelled -> launch nothing
            ok = PV.warmup(Adb(runner=r, cancel=ev), prof, log=lambda m: None, dwell=0, settle=0)
            self.assertFalse(ok)
            self.assertEqual(r.launched, [])

    def test_refuses_on_the_golden_master(self):
        """F5: ticking Warm up + 'Apply to ALL' with the golden on the bench must never open apps on it —
        the golden is never sealed/scrubbed, so any damage rides the next ① Save into every future unit."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            r = self.WarmRunner(golden=True)
            ok, logs = self._warm(prof, r)
            self.assertFalse(ok)
            self.assertEqual(r.launched, [])
            self.assertTrue(any("REFUSING" in m and "golden" in m for m in logs))

    def test_requires_root_so_the_golden_probe_is_never_skipped(self):
        """The golden guard needs su, and is_golden() is FAIL-CLOSED (a blocked su reads as 'golden').
        Both ways of dodging that are broken: probing WITHOUT confirming root gives a false golden-lock
        refusal on every real unit, and SKIPPING the probe when root is absent warms the MASTER. So warm-up
        requires root outright, like provision() — an unrooted unit is refused with an actionable message,
        never warmed on a guess. `su_blocked` models the real cause (a Magisk shell grant that didn't
        survive the Download reboot)."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            for r in (self.WarmRunner(su_blocked=True), self.WarmRunner(root=False)):
                ok, logs = self._warm(prof, r)
                self.assertFalse(ok)
                self.assertEqual(r.launched, [])                      # nothing opened on an unverified unit
                self.assertTrue(any("no root" in m for m in logs), logs)

    def test_an_unrooted_golden_is_never_warmed(self):
        """The regression this requires-root rule exists to prevent: with the probe merely GATED on root
        (`is_root() and is_golden()`), an unrooted golden master sails through the guard and gets all ~14
        of its apps opened — dirtying the master's first-run state and recents. It is never sealed, so it
        is never scrubbed, and the damage rides the next ① Save into EVERY future unit's payload."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            r = self.WarmRunner(golden=True, su_blocked=True)   # the master, its shell grant gone
            ok, _ = self._warm(prof, r)
            self.assertFalse(ok)
            self.assertEqual(r.launched, [])                    # the master was NOT warmed

    def test_warmup_all_reports_the_golden_as_fail_never_skip_golden(self):
        """The golden is a FAIL here, NOT the 'skip-golden' root_all/seal_all use — deliberately, and the
        next test is why. skip-golden SURVIVES the chain; fail drops the unit before Lock. Since is_golden()
        is fail-closed, only the fail mapping keeps a misread on the safe side."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            r = self.WarmRunner(golden=True)
            res = PV.warmup_all(lambda s: Adb(serial=s, runner=r), [("G1", "device")],
                                log=lambda m: None, profile=prof, parallel=False, dwell=0, settle=0)
            self.assertEqual(res["G1"][0], "fail")
            self.assertIn("golden", res["G1"][1].lower())
            self.assertEqual(r.launched, [])             # the master was not warmed

    def test_a_garbled_golden_probe_fails_safe_and_never_survives_to_lock(self):
        """is_golden() is FAIL-CLOSED: one garbled `su` on a perfectly NORMAL unit answers 'golden'. That
        misread must land on the fail-SAFE side. This is the whole reason warm-up reports a golden as `fail`
        rather than `skip-golden` — skip-golden survives _run_chain_core, so the misread would sail on to
        Lock, get sealed un-warmed, and be reported GREEN: the exact defect this feature removes. Root is
        fine here; only the .cas_golden probe garbles."""
        class GarbledGolden(self.WarmRunner):
            def __call__(self, args, input_text=None, timeout=900):
                if "shell" in args and "/debug_ramdisk/su" in args and ".cas_golden" in args[-1]:
                    return 1, "", "error: closed"        # transport hiccup -> is_golden() reads TRUE
                return super().__call__(args, input_text=input_text, timeout=timeout)
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            r = GarbledGolden()
            res = PV.warmup_all(lambda s: Adb(serial=s, runner=r), [("U1", "device")],
                                log=lambda m: None, profile=prof, parallel=False, dwell=0, settle=0)
            self.assertEqual(res["U1"][0], "fail")       # NOT skip-golden, NOT ok
            self.assertEqual(r.launched, [])
            # and the status the chain drops on: fail/error are the only ones _run_chain_core removes.
            self.assertIn(res["U1"][0], ("fail", "error"))

    def test_a_shell_grant_lost_to_the_download_reboot_is_re_taken_not_fatal(self):
        """Warm-up is the first step to call su after the Download reboot, so it is where a MagiskSU shell
        grant that didn't persist surfaces. Requiring root must NOT mean one non-persistent grant fails
        EVERY unit in the batch at ③: root() already re-takes the grant with no human tap, so warm-up tries
        that before refusing. Here su is dead until grant_shell_root runs, then root works."""
        from cas import config as C
        C.save_config({"auto_grant_shell": True})        # setUp pins it off; this is the case that wants it
        r = self.WarmRunner(root=False)
        def fake_grant(adb, log=print, **kw):
            r.root = True                                # the grant is re-taken -> su answers again
            return True
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            with patch.object(PV, "grant_shell_root", fake_grant):
                ok, logs = self._warm(prof, r)
            self.assertTrue(ok)                          # rescued, not failed
            self.assertIn("org.ppsspp.ppsspp", r.launched)
            self.assertTrue(any("re-taking it" in m for m in logs))

    def test_empty_library_app_set_fails_loudly_even_though_frontends_are_present(self):
        """N1 (CRITICAL) — the REAL scenario: an empty manifest (library drive dropped / corrupt profile)
        on an otherwise NORMALLY-provisioned unit where both frontends (the system launcher + the
        Download-installed ES-DE) ARE present and installed. Before the fix, `_warmup_order` unconditionally
        appended WARMUP_FRONTENDS, so the resolved order was NEVER empty — this pass would silently launch
        just the two frontends and report ok, sealing a unit with all 14 emulators un-warmed. The gate must
        key off the LIBRARY-derived set (`_warmup_pkgs`), independent of what the device happens to carry."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            # NOT make_profile(apps=[]) — its `apps = apps or [...]` treats an empty list as "unset" and
            # silently hands back the DEFAULT 3-app manifest, so the profile would not be empty at all.
            # Rewrite the manifest to one that exists but names no packages (the corrupt/empty case).
            prof.manifest_path.write_bytes(b"# manifest with no apps\n")
            self.assertEqual(prof.pkgs(), [])     # the precondition this test rests on
            r = self.WarmRunner()                 # default: BOTH frontends ARE present on the device
            ok, logs = self._warm(prof, r)
            self.assertFalse(ok)
            self.assertEqual(r.launched, [])      # nothing launched at all -- not even the frontends
            self.assertTrue(any("library-derived app set is EMPTY" in m for m in logs))

    def test_unreadable_manifest_fails_loudly_even_though_frontends_are_present(self):
        """N1 (CRITICAL), second real cause named in the finding: the manifest file itself is missing/
        unreadable (library drive dropped mid-run after preflight passed, or a corrupt profile) rather
        than merely present-but-empty. profile.pkgs() returns [] either way, and — as above — the device
        already carries both frontends, so the old order-emptiness check could never have caught this."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            prof.manifest_path.unlink()           # the manifest vanished after the profile was resolved
            r = self.WarmRunner()                 # frontends ARE present on the device
            ok, logs = self._warm(prof, r)
            self.assertFalse(ok)
            self.assertEqual(r.launched, [])
            self.assertTrue(any("library-derived app set is EMPTY" in m for m in logs))

    def test_launched_zero_fails_even_with_a_nonempty_library_set(self):
        """N4's second net, isolated from N1's set-level gate: the library-derived set is genuinely
        non-empty, but the one app it names isn't actually installed on THIS unit (wrong profile assigned,
        or Download partially failed) — frontends skipped here so only that cause is in play. Launching
        zero apps must still fail loudly even when the library set itself was fine."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            r = self.WarmRunner(absent={"org.ppsspp.ppsspp"})
            ok, logs = self._warm(prof, r, skip=frozenset(PV.WARMUP_FRONTENDS))
            self.assertFalse(ok)
            self.assertEqual(r.launched, [])
            self.assertTrue(any("launched 0" in m for m in logs))

    def test_warmup_opens_only_emulators_plus_frontends(self):
        """Warm-up opens the EMULATORS (EMULATOR_PKGS) plus the frontends last — NOT every app. A non-
        emulator app in the manifest (Steam Link, the Companion, always-install utilities) needs no first-
        run warming, so opening it is wasted bench time and must be skipped."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "com.valvesoftware.steamlink",
                                         "com.github.stenzek.duckstation"])
            r = self.WarmRunner()                       # all listed apps + both frontends present
            ok, logs = self._warm(prof, r)
            self.assertTrue(ok)
            self.assertIn("org.ppsspp.ppsspp", r.launched)               # emulator -> warmed
            self.assertIn("com.github.stenzek.duckstation", r.launched)  # emulator -> warmed
            self.assertNotIn("com.valvesoftware.steamlink", r.launched)  # NON-emulator -> skipped
            # frontends still opened LAST — the indexing pass is the point of warm-up
            self.assertIn("org.es_de.frontend", r.launched)
            self.assertIn("com.handheld.launcher", r.launched)

    def test_warmup_with_no_emulators_warms_frontends_only_and_warns(self):
        """A profile whose apps are all non-emulators is not a hard failure (the library set is readable):
        warm-up opens just the frontends and logs a soft warn, rather than failing the seal."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["com.valvesoftware.steamlink"])   # non-emulator only
            r = self.WarmRunner()
            ok, logs = self._warm(prof, r)
            self.assertTrue(ok)
            self.assertNotIn("com.valvesoftware.steamlink", r.launched)    # not an emulator
            self.assertIn("org.es_de.frontend", r.launched)                # frontends still warmed
            self.assertIn("com.handheld.launcher", r.launched)
            self.assertTrue(any("no emulators" in m for m in logs))

    def test_swept_even_though_it_never_reaches_the_foreground(self):
        """N3: a package must be appended to the sweep list right after `adb.launch()` STARTS it, not
        after a confirmed foreground. An app owned by a first-run SAF/permission dialog (or a slow cold
        start) that never reaches the foreground within the timeout is still RUNNING and must still be
        force-stopped at the end, or it survives into the customer's Android recents."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner(never_fg={"org.ppsspp.ppsspp"})
            with patch.object(PV, "WARMUP_FOREGROUND_TIMEOUT", 0.05):
                ok, _ = self._warm(prof, r)
            self.assertTrue(ok)
            self.assertTrue(any("force-stop org.ppsspp.ppsspp" in c for c in r.cmds()),
                            "ppsspp launched but never reached the foreground -- must still be swept")

    def test_all_apps_launch_but_none_foreground_is_a_warn_not_a_fail(self):
        """N4 (IMPORTANT) — the actual reproduced defect: every app monkey-launches fine (launched > 0)
        but the foreground probe degrades on this ROM (dumpsys not printing topResumedActivity, or a
        foreign first-run dialog owning focus) so NONE confirm. `warmed` staying 0 must NOT hard-fail the
        unit — only `len(launched) == 0` may; this must be a loud [warn], not a fail. Also covers N6: the
        pass must still settle -> sweep -> home rather than bail out early and strand the unit unswept."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp", "org.citra.emu"])
            r = self.WarmRunner(never_fg={"org.ppsspp.ppsspp", "org.citra.emu",
                                          "org.es_de.frontend", "com.handheld.launcher"})
            with patch.object(PV, "WARMUP_FOREGROUND_TIMEOUT", 0.05):
                ok, logs = self._warm(prof, r)
            self.assertTrue(ok)                              # a degraded fg probe must never block a seal
            self.assertEqual(len(r.launched), 4)              # every resolved app DID launch
            joined = "\n".join(logs)
            self.assertIn("[warn]", joined)
            self.assertIn("NONE reached the foreground", joined)
            cmds = r.cmds()
            for pkg in r.launched:                            # N3: swept even though none ever foregrounded
                self.assertTrue(any(f"force-stop {pkg}" in c for c in cmds), f"{pkg} was never swept")
            self.assertIn("android.intent.category.HOME", cmds[-1])   # N6: still ends at home

    def test_homescreen_bundled_apps_are_warmed_too(self):
        """F2: an app homescreen_install_missing() installed (its APK is NOT in the payload, so it's
        never in the manifest) must still be warmed — the exact never-opened-emulator bug on the one
        install path warm-up wouldn't otherwise cover."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            hs = prof.payload / "homescreen" / "apps" / "com.retroarch.aarch64"
            hs.mkdir(parents=True)
            (hs / "base.apk").write_text("x")
            r = self.WarmRunner()
            ok, _ = self._warm(prof, r)
            self.assertTrue(ok)
            self.assertIn("com.retroarch.aarch64", r.launched)
            self.assertEqual(r.launched[-2:], ["org.es_de.frontend", "com.handheld.launcher"])

    def test_missing_homescreen_apps_dir_is_not_a_crash(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])           # no homescreen/apps dir at all
            r = self.WarmRunner()
            ok, _ = self._warm(prof, r)
            self.assertTrue(ok)
            self.assertEqual(r.launched[0], "org.ppsspp.ppsspp")

    def test_homescreen_app_already_in_manifest_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            hs = prof.payload / "homescreen" / "apps" / "org.ppsspp.ppsspp"
            hs.mkdir(parents=True)
            (hs / "base.apk").write_text("x")
            r = self.WarmRunner()
            ok, _ = self._warm(prof, r)
            self.assertTrue(ok)
            self.assertEqual(r.launched.count("org.ppsspp.ppsspp"), 1)

    def test_warmup_all_reports_ok_per_device(self):
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            res = PV.warmup_all(lambda s: Adb(serial=s, runner=self.WarmRunner()),
                                [("S1", "device"), ("S2", "device")],
                                log=lambda m: None, profile=prof, parallel=False, dwell=0, settle=0)
            self.assertEqual({k: v[0] for k, v in res.items()}, {"S1": "ok", "S2": "ok"})

    def test_warmup_all_reports_fail_not_cancelled_when_the_pass_warms_nothing(self):
        """F1b: an empty/unreachable-library pass must map to ('fail', reason) — not ('cancelled', …),
        which is what warmup_all assumed before it checked adb.cancel."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            absent_all = {"org.ppsspp.ppsspp", "org.es_de.frontend", "com.handheld.launcher"}
            res = PV.warmup_all(lambda s: Adb(serial=s, runner=self.WarmRunner(absent=absent_all)),
                                [("S1", "device")], log=lambda m: None, profile=prof, parallel=False,
                                dwell=0, settle=0)
            status, detail = res["S1"][:2]            # failures now carry a 3rd Recovery element
            self.assertEqual(status, "fail")
            self.assertIn("launched 0", detail)

    def test_warmup_all_reports_fail_when_warmup_returns_false_without_a_cancel(self):
        """F1b, unit-level: whatever reason warmup() bailed for (golden guard / empty set / zero warmed),
        as long as adb.cancel is NOT set the batch must report 'fail', carrying the last logged line."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            def fake_warmup(adb, profile, log=print, **kw):
                log("FAILING: warmed 0 of 3 app(s) — every one was absent.")
                return False
            with patch.object(PV, "warmup", fake_warmup):
                res = PV.warmup_all(lambda s: Adb(serial=s, runner=self.WarmRunner()),
                                    [("S1", "device")], log=lambda m: None, profile=prof, parallel=False)
            status, detail = res["S1"][:2]            # failures now carry a 3rd Recovery element
            self.assertEqual(status, "fail")
            self.assertIn("warmed 0", detail)

    def test_warmup_all_distinguishes_cancel_from_fail_after_the_call(self):
        """F1b: a cancel that happens INSIDE warmup() (after the pre-call check already passed) must
        still map to 'cancelled' — mirroring provision_all's post-call check."""
        with tempfile.TemporaryDirectory() as t:
            prof = make_profile(t, apps=["org.ppsspp.ppsspp"])
            ev = threading.Event()
            def fake_warmup(adb, profile, log=print, **kw):
                ev.set()               # simulate: the operator hit Cancel WHILE the pass was running
                return False
            with patch.object(PV, "warmup", fake_warmup):
                res = PV.warmup_all(lambda s: Adb(serial=s, runner=self.WarmRunner(), cancel=ev),
                                    [("S1", "device")], log=lambda m: None, profile=prof, parallel=False)
            self.assertEqual(res["S1"][0], "cancelled")


class TestCliWarmup(unittest.TestCase):
    """`cas.cli warmup` resolves the profile and calls PV.warmup — the CLI mirror of the ③ checkbox."""

    def test_warmup_calls_provision_warmup_and_exits_zero(self):
        from unittest.mock import patch
        import cas.cli as CLI
        import cas.provision as PV_mod
        seen = {}
        with tempfile.TemporaryDirectory() as t:
            make_profile(t, name="odin2mini")
            def fake_warmup(adb, profile, log=print, **kw):
                seen["profile"] = profile.name
                return True
            with patch.object(PV_mod, "warmup", fake_warmup):
                rc = CLI.main(["--library", t, "--adb", "adb", "warmup", "--profile", "odin2mini"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["profile"], "odin2mini")

    def test_warmup_unknown_profile_exits_one(self):
        import cas.cli as CLI
        with tempfile.TemporaryDirectory() as t:
            rc = CLI.main(["--library", t, "--adb", "adb", "warmup", "--profile", "nope"])
        self.assertEqual(rc, 1)

    def test_help_does_not_crash_on_a_legacy_codepage_console(self):
        """A Windows console defaults to cp1252, which can't encode the circled step digits (③ etc.) in
        the CLI help/log text — argparse print_help would die with UnicodeEncodeError (the frozen
        cas.exe --help smoke test). main() must make stdout/stderr UTF-8 so --help works on any console."""
        import io, sys as _sys
        import cas.cli as CLI
        buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        old_out, old_err = _sys.stdout, _sys.stderr
        _sys.stdout = _sys.stderr = buf
        try:
            with self.assertRaises(SystemExit) as cm:      # --help prints then exits 0
                CLI.main(["--help"])
        finally:
            _sys.stdout, _sys.stderr = old_out, old_err
        self.assertEqual(cm.exception.code, 0)             # NOT a UnicodeEncodeError
        buf.flush()
        out = buf.buffer.getvalue().decode("utf-8", errors="replace")
        self.assertIn("warmup", out)                       # the help actually printed
        self.assertIn("③", out)                       # ③ survived to output, not a crash


class TestStageWarmup(unittest.TestCase):
    """F9: `_stage`'s 'warmup' branch is the ONLY GUI -> PV.warmup_all wiring; TestRunChain stubs
    `_stage` itself out entirely, so a signature drift here would only be caught on the bench. Exercise
    the REAL method (App.__new__(App) bypasses Tk __init__, same pattern as TestRunChain._app)."""

    def test_stage_warmup_calls_warmup_all_with_expected_kwargs(self):
        import cas.gui as G
        app = G.App.__new__(G.App)
        app.adb_bin = app.fb_bin = None
        app.profiles_root = "profiles-root"
        app.log = lambda m: None
        seen = {}
        def fake_warmup_all(mk_adb, devs, **kw):
            seen["devs"] = list(devs)
            seen.update(kw)
            return {"S1": ("ok", "p")}
        with patch.object(G.PV, "warmup_all", fake_warmup_all):
            res = app._stage("warmup", ["S1"], {"S1": None}, set(), None)
        self.assertEqual(res, {"S1": ("ok", "p")})
        self.assertEqual(seen["devs"], [("S1", "device")])
        self.assertEqual(seen["root"], "profiles-root")
        self.assertEqual(seen["profile_map"], {"S1": None})
        self.assertTrue(seen["parallel"])
        self.assertIs(seen["log"], app.log)


class TestWarningsWarmupGate(unittest.TestCase):
    """F1c: ③ Warm up reads the library exactly like ② Download does, so it must be gated the same way
    (both are 'block' — an operator can't override an unreachable library / no assigned profile)."""

    def test_warmup_gated_same_as_download_on_library_and_profile(self):
        for code in ("library_unreachable", "no_profile"):
            gates = WARN.CATALOG[code]["gates"]
            self.assertIn("warmup", gates)
            self.assertEqual(gates["warmup"], gates["download"])
            self.assertEqual(gates["warmup"], "block")

    def test_warmup_gated_same_as_download_on_no_golden(self):
        """N5: a profile with no captured golden has no manifest either — the same silent-empty-app-set
        path Download would hit — so `no_golden` must gate warmup at the same severity it gates download."""
        gates = WARN.CATALOG["no_golden"]["gates"]
        self.assertIn("warmup", gates)
        self.assertEqual(gates["warmup"], gates["download"])


class TestUiautoForegroundTimeout(unittest.TestCase):
    """F4: uiauto.foreground() must pass an explicit timeout straight through to adb.shell, so a caller
    inside a bounded poll loop can prevent one wedged dumpsys call from inheriting the runner's 900s
    default. The existing caller (grant_shell_root) passes none, so behavior there is unchanged."""

    class _RecordingAdb:
        def __init__(self):
            self.seen_timeout = "UNSET"

        def shell(self, cmd, timeout=None):
            self.seen_timeout = timeout
            return 0, "topResumedActivity=ActivityRecord{u0 com.foo/.Main}\n", ""

    def test_timeout_passes_through_to_adb_shell(self):
        adb = self._RecordingAdb()
        UI.foreground(adb, timeout=7)
        self.assertEqual(adb.seen_timeout, 7)

    def test_default_timeout_is_none_unbounded(self):
        adb = self._RecordingAdb()
        UI.foreground(adb)                    # no timeout passed -> None, adb.shell's own default applies
        self.assertIsNone(adb.seen_timeout)


class TestCancelSleep(unittest.TestCase):
    """F7: the cancel-aware sleep helper used for both the per-app dwell and the settle."""

    def test_zero_duration_elapses_immediately_when_not_cancelled(self):
        self.assertTrue(PV._cancel_sleep(None, 0))

    def test_already_cancelled_returns_false_immediately(self):
        ev = threading.Event()
        ev.set()
        self.assertFalse(PV._cancel_sleep(ev, 5))

    def test_cancelled_mid_sleep_returns_false_promptly(self):
        ev = threading.Event()
        def canceller():
            time.sleep(0.05)
            ev.set()
        th = threading.Thread(target=canceller)
        t0 = time.monotonic()
        th.start()
        ok = PV._cancel_sleep(ev, 5)
        th.join()
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - t0, 1.0)     # returns promptly, not after the full 5s


class TestProvisionLockdown(unittest.TestCase):
    def setUp(self):
        # CAS_CONFIG isolation: every test below runs provision()'s REAL (non-dry) push path to a
        # success with no isolation of its own — see Finding 2.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    def _profile(self, tmp, flags):
        apps = ["org.es_de.frontend", PV.COMPANION_PKG]
        d = pathlib.Path(tmp) / "p"
        pay = d / "golden_root_payload"
        pay.mkdir(parents=True)
        (d / "profile.meta").write_text("model_match=Odin2 ?Mini\nfrontend=es-de\ncaptured=2026-06-16\n")
        (pay / "pkglist.txt").write_text("\n".join(apps) + "\n")
        (pay / "global.meta").write_text("golden_serial=9C33-6BBD\n")
        for a in apps:
            (pay / a / "apk").mkdir(parents=True)
            (pay / a / "apk" / "base.apk").write_text("x")
            (pay / a / "data.tar").write_text("x")
        P.save_manifest(d / "manifest", apps, flags, header="# p")
        return P.Profile(d)

    def test_download_sets_device_owner_when_lockdown_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile(tmp, {"settings": "on", "lockdown": "on"})
            r = FakeRunner()
            self.assertTrue(PV.provision(Adb(runner=r), prof, log=lambda *_: None))
            self.assertTrue(any("dpm set-device-owner" in c for c in r.cmds()))

    def test_download_skips_device_owner_when_lockdown_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile(tmp, {"settings": "on", "lockdown": "off"})
            r = FakeRunner()
            self.assertTrue(PV.provision(Adb(runner=r), prof, log=lambda *_: None))
            self.assertFalse(any("dpm set-device-owner" in c for c in r.cmds()))

    def test_download_skips_device_owner_by_default(self):
        # Default is OFF: a profile with NO @lockdown flag must NOT make the Companion Device Owner,
        # so units never ship "locked by organization" unless a profile opts in with `@lockdown on`.
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile(tmp, {"settings": "on"})
            r = FakeRunner()
            self.assertTrue(PV.provision(Adb(runner=r), prof, log=lambda *_: None))
            self.assertFalse(any("dpm set-device-owner" in c for c in r.cmds()))

    def test_download_succeeds_even_if_lockdown_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile(tmp, {"settings": "on", "lockdown": "on"})
            r = FakeRunner(do_set_ok=False)          # device not fresh -> lockdown fails
            logs = []
            self.assertTrue(PV.provision(Adb(runner=r), prof, log=logs.append))  # still provisions
            self.assertTrue(any("UN-LOCKED" in m for m in logs))                 # LOUD operator warning fired


class TestDeviceOwner(unittest.TestCase):
    def _adb(self, **kw):
        return Adb(runner=FakeRunner(**kw))

    def test_set_device_owner_success(self):
        a = self._adb(device_owner=False, do_set_ok=True, do_restrict=True)
        self.assertTrue(PV.set_device_owner(a, log=lambda *_: None))
        self.assertTrue(any("dpm set-device-owner" in c for c in a.runner.cmds()))

    def test_set_device_owner_idempotent_when_already_owner(self):
        r = FakeRunner(device_owner=True, do_restrict=True)
        a = Adb(runner=r)
        self.assertTrue(PV.set_device_owner(a, log=lambda *_: None))
        self.assertFalse(any("dpm set-device-owner" in c for c in r.cmds()))  # did not re-set

    def test_set_device_owner_fails_when_not_fresh(self):
        a = self._adb(device_owner=False, do_set_ok=False)
        msgs = []
        self.assertFalse(PV.set_device_owner(a, log=msgs.append))
        self.assertTrue(any("FRESH unit" in m for m in msgs), f"got {msgs}")

    def test_set_device_owner_unknown_admin_reports_bad_companion_build(self):
        # The installed Companion APK lacks the device-admin receiver -> the device returns 'Unknown admin'.
        # That's a wrong/old Companion build, NOT an accounts/'fresh unit' problem — the message must say so.
        err = ("java.lang.IllegalArgumentException: Unknown admin: ComponentInfo{"
               "com.gamecove.gamecove_companion/com.gamecove.gamecove_companion.GcDeviceAdminReceiver}")
        a = self._adb(device_owner=False, do_set_ok=False, do_set_err=err)
        msgs = []
        self.assertFalse(PV.set_device_owner(a, log=msgs.append))
        blob = "\n".join(msgs)
        self.assertIn("GcDeviceAdminReceiver", blob)              # names the missing receiver
        self.assertNotIn("FRESH unit", blob)                     # NOT the accounts/fresh-unit message

    def test_set_device_owner_fails_when_restrictions_missing(self):
        # Monkeypatch sleep to a no-op: the polling loop would otherwise sleep ~3s on the failure path.
        orig_sleep = PV.time.sleep
        PV.time.sleep = lambda *_a, **_k: None
        try:
            a = Adb(runner=FakeRunner(do_set_ok=True, do_restrict=False))
            self.assertFalse(PV.set_device_owner(a, log=lambda *_: None))
        finally:
            PV.time.sleep = orig_sleep

    def test_set_device_owner_confirms_when_restrictions_only_in_dumpsys_user(self):
        # Android 14+ (real MANGMI AIR X): the restrictions ARE applied but the per-admin userRestrictions
        # field in `dumpsys device_policy` is EMPTY — they surface in `dumpsys user` instead. Verification
        # must consult BOTH, else it FALSELY reports 'lockdown FAILED' on a correctly-locked unit.
        a = Adb(runner=FakeRunner(do_set_ok=True, do_restrict=True, restrict_in="user"))
        self.assertTrue(PV.set_device_owner(a, log=lambda *_: None))

    def test_release_sends_token_broadcast_and_confirms_cleared(self):
        r = FakeRunner(device_owner=True, release_clears=True)
        a = Adb(runner=r)
        self.assertTrue(PV.release(a, log=lambda *_: None))
        self.assertTrue(any("am broadcast" in c and "action.RELEASE" in c for c in r.cmds()))
        self.assertTrue(any("gc-release-7f3a9c2e" in c for c in r.cmds()))  # token on the wire

    def test_release_fails_when_owner_not_cleared(self):
        a = Adb(runner=FakeRunner(device_owner=True, release_clears=False))
        self.assertFalse(PV.release(a, log=lambda *_: None))

    def test_release_noop_when_not_owner(self):
        a = Adb(runner=FakeRunner(device_owner=False))
        self.assertTrue(PV.release(a, log=lambda *_: None))


class TestSaveManifestAxes(unittest.TestCase):
    def test_axes_roundtrip(self):
        d = pathlib.Path(tempfile.mkdtemp())
        m = d / "manifest"
        P.save_manifest(m, ["com.foo", "com.bar", "xyz.aethersx2.android"],
                        {"settings": "on"},
                        axes={"com.foo": (True, True), "com.bar": (True, False),
                              "xyz.aethersx2.android": (False, True)})
        self.assertEqual(P.manifest_axes(m),
                         {"com.foo": (True, True), "com.bar": (True, False),
                          "xyz.aethersx2.android": (False, True)})
        self.assertEqual(P.manifest_pkgs(m),
                         ["com.foo", "com.bar", "xyz.aethersx2.android"])

    def test_no_axes_writes_bare_lines(self):
        d = pathlib.Path(tempfile.mkdtemp())
        m = d / "manifest"
        P.save_manifest(m, ["com.foo"], {"settings": "on"})
        self.assertEqual(P.manifest_axes(m), {"com.foo": (True, True)})
        self.assertIn("\ncom.foo\n", "\n" + m.read_text())

    def test_gamelauncher_flag_roundtrips(self):
        import tempfile, pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        m = d / "manifest"
        P.save_manifest(m, ["com.foo"], {"gamelauncher": "on", "homescreen": "on"})
        self.assertEqual(P.manifest_flags(m).get("gamelauncher"), "on")

    def test_initial_capture_selection_round_trips_launcher_flags(self):
        from cas import profiles as P
        apps = ["com.retroarch.aarch64", "com.note.app"]
        # saved: an unticked game launcher (config off), HOME on, a package axis override
        sel = P.initial_capture_selection(
            apps, {"com.note.app": (True, False)},
            {"gamelauncher": "off", "homescreen": "on"},
            game_launcher="com.handheld.launcher", home_launcher="com.android.launcher3")
        self.assertEqual(sel["com.retroarch.aarch64"], (True, True))     # emulator default
        self.assertEqual(sel["com.note.app"], (True, False))             # package axis override applied
        self.assertEqual(sel["com.handheld.launcher"], (False, False))   # saved gamelauncher=off honored
        self.assertEqual(sel["com.android.launcher3"], (False, True))    # saved homescreen=on honored

    def test_launchers_config_on_by_default(self):
        """No saved flags: BOTH the game launcher AND the HOME launcher default to config-ON, so the
        homescreen + emulator picks are captured unless the operator unticks them."""
        from cas import profiles as P
        sel = P.initial_capture_selection(
            [], {}, {}, game_launcher="com.handheld.launcher", home_launcher="com.android.launcher3")
        self.assertEqual(sel["com.handheld.launcher"], (False, True))    # @gamelauncher default on
        self.assertEqual(sel["com.android.launcher3"], (False, True))    # @homescreen default on (was off)
        # default_capture_selection agrees on the HOME launcher default
        self.assertEqual(P.default_capture_selection([], home_launcher="com.android.launcher3")
                         ["com.android.launcher3"], (False, True))


class TestProfileLauncherAndAxes(unittest.TestCase):
    def test_all_pkgs_includes_launcher_meta(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "golden_root_payload").mkdir(parents=True)
        (d / "golden_root_payload" / "pkglist.txt").write_text("com.foo\n")
        (d / "profile.meta").write_text("launcher_pkg=com.handheld.launcher\n")
        prof = P.Profile(d)
        self.assertIn("com.handheld.launcher", prof.all_pkgs())
        self.assertIn("com.foo", prof.all_pkgs())

    def test_launcher_pkg_falls_back_to_homescreen_meta(self):
        # The capture writes launcher_pkg into homescreen/meta (NOT profile.meta). launcher_pkg() must
        # resolve it from there, so the Download list excludes the device's own launcher (it's a system
        # app, never installable) the same way all_pkgs() appends it — the two must never disagree.
        d = pathlib.Path(tempfile.mkdtemp())
        hs = d / "golden_root_payload" / "homescreen"
        hs.mkdir(parents=True)
        (d / "golden_root_payload" / "pkglist.txt").write_text("com.foo\n")
        (d / "profile.meta").write_text("model_match=\n")          # NO launcher_pkg here
        (hs / "meta").write_text("launcher_pkg=com.android.launcher3\nlauncher_uid=10084\n")
        prof = P.Profile(d)
        self.assertEqual(prof.launcher_pkg(), "com.android.launcher3")
        self.assertIn("com.android.launcher3", prof.all_pkgs())     # appended for completeness
        own = [p for p in prof.all_pkgs() if p != prof.launcher_pkg()]
        self.assertNotIn("com.android.launcher3", own)              # …but excluded from the Download rows

    def test_axes_reads_manifest_tokens(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "manifest").write_text("com.foo\nxyz.aethersx2.android config\n")
        prof = P.Profile(d)
        self.assertEqual(prof.axes(), {"com.foo": (True, True),
                                       "xyz.aethersx2.android": (False, True)})


class TestSealScrub(unittest.TestCase):
    def test_seal_runs_scrub_before_unroot(self):
        ra, fb = FakeRunner(), FbRunner()
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            stock = f.name
        try:
            ok = PV.seal(Adb(runner=ra), Fastboot(runner=fb), stock, log=lambda m: None, wait=False)
        finally:
            os.unlink(stock)
        self.assertTrue(ok)
        cmds = ra.cmds()
        scrub_i = next((i for i, c in enumerate(cmds) if "scrub.sh" in c), None)
        magisk_i = next((i for i, c in enumerate(cmds) if "uninstall com.topjohnwu.magisk" in c), None)
        self.assertIsNotNone(scrub_i, "scrub.sh was never run during seal")
        self.assertIsNotNone(magisk_i)
        # scrub runs BEFORE the Magisk uninstall, which itself precedes the un-root flash
        self.assertLess(scrub_i, magisk_i, "scrub must run before the un-root steps")
        self.assertIn("flash init_boot", "\n".join(fb.cmds()))


class TestEdlFailFast(unittest.TestCase):
    """root_all/seal_all must fail-fast (no doomed fastboot flash) when an EDL-only unit (MANGMI,
    detected via ro.mangmi.dev.code) resolves to NO firmware build — unless it's explicitly pinned
    to the default kit. Non-EDL units keep the bundled-default fastboot fallback (covered elsewhere)."""

    NO_FW = {"firmware_id": None, "version": None, "manual": False,
             "suggested": None, "ok": False, "warnings": [], "firmware": None}

    def _pin_default(self):
        from cas import firmware as FW
        return {"firmware_id": FW.DEFAULT_FW_ID, "version": None, "manual": True,
                "suggested": None, "ok": True, "warnings": [], "firmware": None}

    def _prof(self, t):
        prof = make_profile(t, "p", "AIR X")
        (prof.path / "profile.meta").write_text("model_match=AIR X\n")   # default-kit stock, no override
        return prof

    def _edl_adb(self, s):
        return Adb(serial=s, runner=FakeRunner(model="AIR X", root=True, dev_code="MQ66"))

    def test_edl_only_device_helper(self):
        from cas import firmware as FW
        self.assertTrue(FW.edl_only_device({"dev_code": "MQ66"}))
        self.assertFalse(FW.edl_only_device({"dev_code": ""}))
        self.assertFalse(FW.edl_only_device({"dev_code": "   "}))
        self.assertFalse(FW.edl_only_device({}))
        self.assertFalse(FW.edl_only_device(None))

    def test_root_all_fails_fast_on_edl_unit_without_firmware(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            prof = self._prof(t)
            with mock.patch("cas.firmware.resolve", return_value=self.NO_FW):
                res = PV.root_all(
                    self._edl_adb, lambda s: Fastboot(serial=s, runner=FbRunner()),
                    [("MQ66X", "device")], profiles_root=t, appdir=t, log=lambda m: None, profile=prof)
            # Guard fires before root(): without it, root=True would return ("ok", …).
            self.assertEqual(res["MQ66X"][0], "fail")
            self.assertIn("firmware", res["MQ66X"][1].lower())

    def test_seal_all_fails_fast_on_edl_unit_without_firmware(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            prof = self._prof(t)
            with mock.patch("cas.firmware.resolve", return_value=self.NO_FW):
                res = PV.seal_all(
                    self._edl_adb, lambda s: Fastboot(serial=s, runner=FbRunner()),
                    [("MQ66X", "device")], profiles_root=t, appdir=t, log=lambda m: None, profile=prof)
            self.assertEqual(res["MQ66X"][0], "fail")
            self.assertIn("firmware", res["MQ66X"][1].lower())

    def test_root_all_default_kit_pin_bypasses_edl_guard(self):
        # An operator who EXPLICITLY pinned the bundled default kit must NOT be blocked — respect the pin.
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            prof = self._prof(t)
            with mock.patch("cas.firmware.resolve", return_value=self._pin_default()):
                res = PV.root_all(
                    self._edl_adb, lambda s: Fastboot(serial=s, runner=FbRunner()),
                    [("MQ66X", "device")], profiles_root=t, appdir=t, log=lambda m: None, profile=prof)
            self.assertEqual(res["MQ66X"][0], "ok")   # default-kit pin → fastboot path → already-rooted 'ok'


class TestRunChain(unittest.TestCase):
    def _app(self):
        from cas.gui import App
        app = App.__new__(App)                            # bypass Tk __init__
        app.adb_bin = app.fb_bin = None
        app.profiles_root = "."
        app.assigned = {"S1": "p", "S2": "p"}
        app.assigned_manual = set()                       # required by _profile_map
        app.log = lambda m: None                          # required by _run_chain_core logging
        app.cancel_event = type("E", (), {"is_set": lambda self: False})()
        app._stage_calls = []
        def fake_stage(step, serials, pm, force, cev, wait_boot=False):
            app._stage_calls.append((step, list(serials), wait_boot))
            # S1 fails 'root'; everything else ok
            return {s: (("fail" if (step == "root" and s == "S1") else "ok"),) for s in serials}
        app._stage = fake_stage
        return app

    def test_failed_root_drops_from_download(self):
        app = self._app()
        survivors = app._run_chain_core(["root", "download", "lock"], ["S1", "S2"], None)
        # stages run in order; download/lock only see S2 (S1 dropped after failing root). Download gets
        # wait_boot=True because Lock follows it (so seal never starts on a still-rebooting unit).
        self.assertEqual(app._stage_calls,
                         [("root", ["S1", "S2"], False), ("download", ["S2"], True), ("lock", ["S2"], False)])
        self.assertEqual(survivors, ["S2"])

    def test_download_as_last_step_does_not_wait_for_boot(self):
        app = self._app()
        app._run_chain_core(["root", "download"], ["S2"], None)   # S2 passes root; download is last
        self.assertEqual(app._stage_calls, [("root", ["S2"], False), ("download", ["S2"], False)])

    def test_save_step_root_fails_no_indexerror(self):
        """Bug-repro (fix #1): survivors==[] after root failure must NOT raise IndexError on survivors[0]."""
        from unittest.mock import patch
        import cas.provision as PV_mod
        app = self._app()
        # override _stage so root ALWAYS fails for S1 (our only device)
        def fake_stage_all_fail(step, serials, pm, force, cev, wait_boot=False):
            app._stage_calls.append((step, list(serials)))
            return {s: ("fail",) for s in serials}
        app._stage = fake_stage_all_fail
        captured = []
        with patch.object(PV_mod, "capture_to_pc", lambda *a, **kw: captured.append(True) or True):
            # Before fix #1 this raises IndexError; after fix it must return []
            survivors = app._run_chain_core(["root", "save"], ["S1"], "testprof")
        self.assertEqual(survivors, [])
        self.assertEqual(captured, [])  # capture_to_pc must never be invoked

    def test_save_step_root_succeeds_invokes_capture(self):
        """When root succeeds for the single device, capture_to_pc must be called on that device."""
        from unittest.mock import patch
        import cas.provision as PV_mod
        app = self._app()
        # override _stage so root SUCCEEDS for S1
        def fake_stage_ok(step, serials, pm, force, cev, wait_boot=False):
            app._stage_calls.append((step, list(serials)))
            return {s: ("ok",) for s in serials}
        app._stage = fake_stage_ok
        captured_serials = []
        def fake_capture(adb, name, stamp, root, log):
            captured_serials.append(adb.serial)
            return True
        with patch.object(PV_mod, "capture_to_pc", fake_capture):
            survivors = app._run_chain_core(["root", "save"], ["S1"], "testprof")
        self.assertEqual(survivors, ["S1"])
        self.assertEqual(captured_serials, ["S1"])

    def test_warmup_runs_between_download_and_lock(self):
        app = self._app()
        survivors = app._run_chain_core(["root", "download", "warmup", "lock"], ["S1", "S2"], None)
        # S1 fails root and is dropped; warm-up sits between Download and Lock. Download gets
        # wait_boot=True because warm-up follows it — a warm-up must never touch a rebooting unit.
        self.assertEqual(app._stage_calls,
                         [("root", ["S1", "S2"], False), ("download", ["S2"], True),
                          ("warmup", ["S2"], False), ("lock", ["S2"], False)])
        self.assertEqual(survivors, ["S2"])

    def test_download_waits_for_boot_when_only_warmup_follows(self):
        """Regression guard: wait_boot keys off 'any step follows', not off Lock specifically."""
        app = self._app()
        app._run_chain_core(["download", "warmup"], ["S2"], None)
        self.assertEqual(app._stage_calls, [("download", ["S2"], True), ("warmup", ["S2"], False)])


class TestRunChainSaveSelectionGuard(unittest.TestCase):
    """BLOCKING FINDING 1 regression: ▶ Run with ① Save ticked on a MULTI-selection used to silently
    target only `_selected_serial()` (the topmost row) — the footer said 'N of M devices selected', but
    Save ran on exactly ONE of them with no warning, and the rest got nothing. The footer's blast-radius
    promise must never disagree with what ▶ Run actually does, so this must refuse (not proceed on one
    device) whenever the selection isn't exactly one row."""

    def _app(self, selection):
        from cas.gui import App
        from unittest import mock
        import types
        app = App.__new__(App)                              # bypass Tk __init__
        app.dev_tree = types.SimpleNamespace(selection=lambda: list(selection))
        # Only ① Save is ticked (root/download/warmup/lock all off) — mirrors chain_vars' BooleanVars.
        app.chain_vars = {k: types.SimpleNamespace(get=lambda k=k: k == "save")
                           for k in ("root", "save", "download", "warmup", "lock")}
        app._run_save_calls = []
        app._run_save = lambda steps, serial: app._run_save_calls.append((list(steps), serial))
        # The non-save branch (_action_targets/_preflight/_run_chain) must NEVER be reached from here —
        # assert_not_called() below proves it.
        app._action_targets = mock.Mock(side_effect=AssertionError("must not reach the non-save branch"))
        return app

    def test_multi_selection_with_save_ticked_refuses_to_run_on_one_device(self):
        from unittest import mock
        import cas.gui as G
        app = self._app(["S1", "S2", "S3"])
        with mock.patch.object(G.messagebox, "showinfo") as info:
            app.run_chain()
        info.assert_called_once()
        self.assertIn("ONE", info.call_args[0][1])
        self.assertEqual(app._run_save_calls, [],
                         "Save must NOT silently proceed on just the topmost device of a multi-selection")
        app._action_targets.assert_not_called()

    def test_zero_selection_with_save_ticked_refuses_to_run(self):
        from unittest import mock
        import cas.gui as G
        app = self._app([])
        with mock.patch.object(G.messagebox, "showinfo") as info:
            app.run_chain()
        info.assert_called_once()
        self.assertEqual(app._run_save_calls, [])

    def test_single_selection_with_save_ticked_runs_on_that_one_device(self):
        from unittest import mock
        import cas.gui as G
        app = self._app(["S1"])
        with mock.patch.object(G.messagebox, "showinfo") as info:
            app.run_chain()
        info.assert_not_called()
        self.assertEqual(app._run_save_calls, [(["save"], "S1")])


class TestSealSelected(unittest.TestCase):
    """Settings ▸ 'Seal selected unit' — single-device slice of ③ Lock via PV.seal_all."""

    def _app(self, serial="S1"):
        from cas.gui import App
        app = App.__new__(App)                    # bypass Tk __init__
        app.adb_bin = app.fb_bin = None
        app.profiles_root = "."
        app.assigned = {"S1": "p"}
        app.assigned_manual = set()               # S1 not hand-assigned → force stays empty
        app.cancel_event = None
        app.log = lambda m: None
        app._on_flash_critical = lambda active: None
        app.refresh_devices = lambda: None
        # win.after(0, cb) must invoke cb (work() calls self.win.after(0, self.refresh_devices))
        app.win = type("W", (), {"after": lambda self, ms, cb=None: (cb() if cb else None)})()
        app._selected_serial = lambda: serial
        # _run_bg: run the work fn synchronously so the seal_all call happens in-test
        app._bg = []
        app._run_bg = lambda fn, label=None: app._bg.append((label, fn()))
        return app

    def test_no_selection_shows_info_and_does_not_seal(self):
        from unittest import mock
        import cas.gui as G
        app = self._app(serial=None)
        with mock.patch.object(G.messagebox, "showinfo") as info, \
             mock.patch.object(G.PV, "seal_all") as seal:
            app.seal_selected()
        info.assert_called_once()
        seal.assert_not_called()

    def test_confirm_no_does_not_seal(self):
        from unittest import mock
        import cas.gui as G
        app = self._app()
        with mock.patch.object(G.messagebox, "askyesno", return_value=False), \
             mock.patch.object(G.PV, "seal_all") as seal:
            app.seal_selected()
        seal.assert_not_called()

    def test_confirm_yes_seals_the_one_selected_unit(self):
        from unittest import mock
        import cas.gui as G
        app = self._app()
        rec = {}
        def fake_seal_all(mk_adb, mk_fb, devices, **kw):
            rec["devices"] = list(devices)
            rec["profile_map"] = kw.get("profile_map")
            rec["force"] = kw.get("force_serials")
            return {"S1": ("ok", "p")}
        with mock.patch.object(G.messagebox, "askyesno", return_value=True), \
             mock.patch.object(G.PV, "seal_all", side_effect=fake_seal_all):
            app.seal_selected()
        self.assertEqual(rec["devices"], [("S1", "device")])   # only the selected unit
        self.assertIn("S1", rec["profile_map"])                # resolved via _profile_map
        self.assertEqual(rec["force"], set())                  # S1 not hand-assigned
        self.assertEqual(app._bg[0][1], {"S1": ("ok", "p")})   # work() returns the report dict


class TestResolveChain(unittest.TestCase):
    def _r(self, **t):
        from cas.gui import App                          # the main window class
        return App._resolve_chain(None, t)               # pure: no self state used

    def test_orders_unit_chain(self):
        self.assertEqual(self._r(lock=True, root=True, download=True), (["root", "download", "lock"], None))

    def test_golden_chain(self):
        self.assertEqual(self._r(root=True, save=True), (["root", "save"], None))

    def test_save_excludes_download_lock(self):
        steps, err = self._r(save=True, download=True)
        self.assertEqual(steps, [])
        self.assertIn("Save", err)

    def test_nothing_ticked_is_error(self):
        steps, err = self._r()
        self.assertEqual(steps, [])
        self.assertTrue(err)

    def test_orders_warmup_between_download_and_lock(self):
        self.assertEqual(self._r(lock=True, warmup=True, download=True, root=True),
                         (["root", "download", "warmup", "lock"], None))

    def test_warmup_alone_is_valid(self):
        self.assertEqual(self._r(warmup=True), (["warmup"], None))

    def test_save_excludes_warmup(self):
        steps, err = self._r(save=True, warmup=True)
        self.assertEqual(steps, [])
        self.assertIn("Save", err)


class TestModalManifestTransforms(unittest.TestCase):
    """Pure axes→manifest transforms behind the run-time app-pick modal (no Tk)."""

    def test_manifest_from_axes_includes_when_either_axis_on(self):
        from cas.gui import _manifest_from_axes
        axes = {"a": (True, True), "b": (False, True), "c": (True, False), "d": (False, False)}
        pkgs, sub = _manifest_from_axes(axes)
        self.assertEqual(pkgs, ["a", "b", "c"])            # d (both off) excluded
        self.assertEqual(sub, {"a": (True, True), "b": (False, True), "c": (True, False)})


class TestPickCapture(unittest.TestCase):
    """_pick_capture: the Save modal's behavior choices (incl. hardening) land in the capture-manifest and
    then seed the Download defaults."""

    def setUp(self):
        self._prev_cfg = os.environ.get("CAS_CONFIG")
        self._cfgdir = tempfile.TemporaryDirectory()
        os.environ["CAS_CONFIG"] = str(pathlib.Path(self._cfgdir.name) / "cas-config.json")

    def tearDown(self):
        if self._prev_cfg is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._prev_cfg
        self._cfgdir.cleanup()

    def test_hardening_flows_to_capture_manifest_and_seeds_download(self):
        from cas.gui import App
        root = pathlib.Path(tempfile.mkdtemp())
        d = root / "prof"
        (d / "golden_root_payload").mkdir(parents=True)
        (d / "golden_root_payload" / "pkglist.txt").write_text("com.a\n")
        (d / "profile.meta").write_text("")
        app = App.__new__(App)
        app.profiles_root = str(root)
        app.log = lambda m: None
        app._scan_device_apps = lambda s: []
        app._detect_device_launchers = lambda s: (None, None)
        app._row_model = lambda s: "AIR X"
        seen = {}
        def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None, flags_caption="—",
                       apk_locked=None):
            seen["flag_keys"] = [f[0] for f in flag_specs]
            # operator unticks hardening at Save
            return ({}, {"settings": "on", "hardening": "off", "grants": "on"})
        app._app_pick_modal = fake_modal
        self.assertTrue(app._pick_capture("S1", "prof"))
        # the Save modal offered settings/hardening/grants (the optimization stuff is THERE)
        self.assertIn("hardening", seen["flag_keys"])
        # capture-manifest carries the choice
        self.assertEqual(P.manifest_flags(d / "capture-manifest")["hardening"], "off")
        # and it seeds the Download default
        PV.seed_default_manifest(d, "prof")
        self.assertEqual(P.manifest_flags(d / "manifest")["hardening"], "off")

    def test_launchers_are_behavior_flags_not_app_rows(self):
        # When launchers are detected they appear as @gamelauncher/@homescreen BEHAVIOR FLAGS, not app
        # rows; their pkgs never become manifest package lines, and the modal's flag choice is captured.
        from cas.gui import App
        root = pathlib.Path(tempfile.mkdtemp())
        d = root / "prof"
        (d / "golden_root_payload").mkdir(parents=True)
        (d / "golden_root_payload" / "pkglist.txt").write_text("org.ppsspp.ppsspp\n")
        (d / "profile.meta").write_text("")
        app = App.__new__(App)
        app.profiles_root = str(root)
        app.log = lambda m: None
        app._scan_device_apps = lambda s: ["org.ppsspp.ppsspp"]
        app._detect_device_launchers = lambda s: ("com.handheld.launcher", "com.android.launcher3")
        app._row_model = lambda s: "AIR X"
        seen = {}
        def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None, flags_caption="—",
                       apk_locked=None):
            seen["row_pkgs"] = list(rows.keys())
            seen["flag_keys"] = [f[0] for f in flag_specs]
            return ({p: (True, True) for p in rows}, {"settings": "on", "hardening": "on", "grants": "on",
                                                      "homescreen": "off", "gamelauncher": "on"})
        app._app_pick_modal = fake_modal
        self.assertTrue(app._pick_capture("S1", "prof"))
        # launchers are NOT app rows…
        self.assertNotIn("com.handheld.launcher", seen["row_pkgs"])
        self.assertNotIn("com.android.launcher3", seen["row_pkgs"])
        self.assertEqual(seen["row_pkgs"], ["org.ppsspp.ppsspp"])
        # …they're behavior flags
        self.assertIn("gamelauncher", seen["flag_keys"])
        self.assertIn("homescreen", seen["flag_keys"])
        # the launcher pkgs never become manifest package lines; the flag choice is captured
        man = d / "capture-manifest"
        self.assertNotIn("com.handheld.launcher", P.manifest_pkgs(man))
        self.assertNotIn("com.android.launcher3", P.manifest_pkgs(man))
        self.assertEqual(P.manifest_flags(man)["homescreen"], "off")
        self.assertEqual(P.manifest_flags(man)["gamelauncher"], "on")

    def test_homescreen_always_shown_gamelauncher_only_when_detected(self):
        # @homescreen is ALWAYS offered (capture.sh resolves the HOME launcher itself); @gamelauncher is
        # offered ONLY when a game frontend is detected.
        from cas.gui import App
        root = pathlib.Path(tempfile.mkdtemp())
        d = root / "prof"
        (d / "golden_root_payload").mkdir(parents=True)
        (d / "golden_root_payload" / "pkglist.txt").write_text("com.a\n")
        (d / "profile.meta").write_text("")
        app = App.__new__(App)
        app.profiles_root = str(root)
        app.log = lambda m: None
        app._scan_device_apps = lambda s: []
        app._row_model = lambda s: "AIR X"
        keys = {}
        def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None, flags_caption="—",
                       apk_locked=None):
            keys["k"] = [f[0] for f in flag_specs]
            return ({}, {})
        app._app_pick_modal = fake_modal
        # NO launcher detected at all → homescreen still shown, gamelauncher hidden
        app._detect_device_launchers = lambda s: (None, None)
        app._pick_capture("S1", "prof")
        self.assertIn("homescreen", keys["k"])
        self.assertNotIn("gamelauncher", keys["k"])
        # game frontend detected (home not) → both shown
        app._detect_device_launchers = lambda s: ("com.handheld.launcher", None)
        app._pick_capture("S1", "prof")
        self.assertIn("homescreen", keys["k"])
        self.assertIn("gamelauncher", keys["k"])


class TestPickDownloads(unittest.TestCase):
    """_pick_downloads: one modal per DISTINCT assigned profile, write-after-all, cancel aborts clean."""

    def setUp(self):
        self._prev_cfg = os.environ.get("CAS_CONFIG")
        self._cfgdir = tempfile.TemporaryDirectory()
        os.environ["CAS_CONFIG"] = str(pathlib.Path(self._cfgdir.name) / "cas-config.json")

    def tearDown(self):
        if self._prev_cfg is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._prev_cfg
        self._cfgdir.cleanup()

    def _profile(self, root, name, pkgs):
        d = pathlib.Path(root) / name
        (d / "golden_root_payload").mkdir(parents=True)
        (d / "golden_root_payload" / "pkglist.txt").write_text("\n".join(pkgs) + "\n")
        (d / "profile.meta").write_text("")
        return d

    def _app(self, root):
        from cas.gui import App
        app = App.__new__(App)                             # bypass Tk __init__
        app.profiles_root = str(root)
        app.log = lambda m: None
        return app

    def test_one_modal_per_distinct_profile_writes_after_all(self):
        root = pathlib.Path(tempfile.mkdtemp())
        _saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES")}
        os.environ["CAS_CONFIG"] = str(root / "cfg.json")
        os.environ["CAS_PROFILES"] = str(root)             # isolate apk_store_dir (else store apps leak into the manifest)
        try:
            self._profile(root, "p", ["com.foo", "com.bar"])
            app = self._app(root)
            app.assigned = {"S1": "p", "S2": "p"}              # two devices, one shared profile
            calls = []
            def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None, cfg_disabled=None,
                          apk_locked=None):
                calls.append(title)
                return ({pk: (True, False) for pk in rows}, {"settings": "on"})
            app._app_pick_modal = fake_modal
            self.assertTrue(app._pick_downloads(["S1", "S2"]))
            self.assertEqual(len(calls), 1)                    # ONE modal for the shared profile
            m = root / "p" / "manifest"
            self.assertEqual(P.manifest_pkgs(m), ["com.foo", "com.bar"])
        finally:
            for _k, _v in _saved.items():
                os.environ.pop(_k, None) if _v is None else os.environ.__setitem__(_k, _v)

    def test_cancel_aborts_with_no_writes(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self._profile(root, "p1", ["com.foo"])
        self._profile(root, "p2", ["com.baz"])
        app = self._app(root)
        app.assigned = {"S1": "p1", "S2": "p2"}            # two distinct profiles
        seen = []
        def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None, cfg_disabled=None,
                       apk_locked=None):
            seen.append(title)
            return None if len(seen) == 2 else ({pk: (True, True) for pk in rows}, {})
        app._app_pick_modal = fake_modal
        self.assertFalse(app._pick_downloads(["S1", "S2"]))   # cancel on the 2nd modal
        # write-after-all: a late cancel leaves NEITHER profile's manifest written
        self.assertFalse((root / "p1" / "manifest").exists())
        self.assertFalse((root / "p2" / "manifest").exists())

    def test_skips_unassigned_and_no_match(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self._profile(root, "p", ["com.foo"])
        app = self._app(root)
        app.assigned = {"S1": "p", "S2": "(no match)"}     # S3 has no entry at all
        calls = []
        def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None, cfg_disabled=None,
                       apk_locked=None):
            calls.append(title)
            return ({pk: (True, True) for pk in rows}, {})
        app._app_pick_modal = fake_modal
        self.assertTrue(app._pick_downloads(["S1", "S2", "S3"]))
        self.assertEqual(len(calls), 1)                    # only the real profile prompts

    def test_modal_gets_golden_driven_rows_and_disabled_config(self):
        """Wiring: a captured app (with config) is pre-ticked; an apk-only capture has Config disabled; a
        store-only app is listed un-ticked with Config disabled (it wasn't in the golden)."""
        root = pathlib.Path(tempfile.mkdtemp())
        _saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES")}
        os.environ["CAS_CONFIG"] = str(root / "cfg.json")
        os.environ["CAS_PROFILES"] = str(root)             # apk_store_dir -> root/_apks (we seed it below)
        try:
            d = self._profile(root, "p", ["com.withcfg", "com.apkonly"])
            pay = d / "golden_root_payload"
            (pay / "com.withcfg" / "apk").mkdir(parents=True)      # captured apk + config
            (pay / "com.withcfg" / "apk" / "base.apk").write_text("x")
            (pay / "com.withcfg" / "data.tar").write_text("x")
            (pay / "com.apkonly" / "apk").mkdir(parents=True)      # captured apk-only (no data.tar)
            (pay / "com.apkonly" / "apk" / "base.apk").write_text("x")
            _seed_store(root / "_apks", "com.storeonly", "v1")     # store-only managed app
            app = self._app(root)
            app.assigned = {"S1": "p"}
            seen = {}
            def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None, cfg_disabled=None,
                          apk_locked=None):
                seen["rows"], seen["cfg_disabled"] = rows, cfg_disabled
                return ({pk: rows[pk] for pk in rows}, {})  # accept the proposed defaults
            app._app_pick_modal = fake_modal
            self.assertTrue(app._pick_downloads(["S1"]))
            self.assertEqual(seen["rows"]["com.withcfg"], (True, True))
            self.assertEqual(seen["rows"]["com.apkonly"], (True, False))
            self.assertEqual(seen["rows"]["com.storeonly"], (False, False))
            self.assertEqual(seen["cfg_disabled"], {"com.apkonly", "com.storeonly"})
            # accepting the defaults installs only the golden apps; the store-only app is NOT written
            self.assertEqual(sorted(P.manifest_pkgs(d / "manifest")), ["com.apkonly", "com.withcfg"])
        finally:
            for _k, _v in _saved.items():
                os.environ.pop(_k, None) if _v is None else os.environ.__setitem__(_k, _v)

    def test_always_install_app_passed_to_modal_as_apk_locked(self):
        # An always-install app that IS a row must reach the modal as apk_locked (APK forced-on + not
        # untickable / not cleared by Deselect-all) — the guarantee survives moving the checkbox out.
        root = pathlib.Path(tempfile.mkdtemp())
        _saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES")}
        os.environ["CAS_CONFIG"] = str(root / "cfg.json")
        os.environ["CAS_PROFILES"] = str(root)
        try:
            from cas import config as C
            self._profile(root, "p", ["com.foo", "com.always"])
            C.set_always_install_pkgs(["com.always"])
            app = self._app(root)
            app.assigned = {"S1": "p"}
            seen = {}
            def fake_modal(title, intro, prof, rows, launchers, flag_specs, labels=None, cfg_disabled=None,
                           apk_locked=None):
                seen["apk_locked"] = apk_locked
                return ({pk: (True, True) for pk in rows}, {})
            app._app_pick_modal = fake_modal
            self.assertTrue(app._pick_downloads(["S1"]))
            self.assertIn("com.always", seen["apk_locked"])
            self.assertNotIn("com.foo", seen["apk_locked"])
        finally:
            for _k, _v in _saved.items():
                os.environ.pop(_k, None) if _v is None else os.environ.__setitem__(_k, _v)


class TestDetectLaunchers(unittest.TestCase):
    """_detect_device_launchers pushes lib-root.sh first (modal opens before capture pushes it), resolves
    HOME without root and the GAME frontend with root, falling back to a plain shell when su is blocked."""

    class _FakeAdb:
        def __init__(self, serial=None, adb=None, home="com.android.launcher3",
                     game_su="com.handheld.launcher", game_shell="", push_ok=True):
            self._home, self._game_su, self._game_shell, self._push_ok = home, game_su, game_shell, push_ok
            self.calls = []
        def shell(self, cmd):
            self.calls.append(("shell", cmd))
            if "home_launcher" in cmd:
                return (0, self._home + "\n", "")
            if "game_launcher" in cmd:
                return (0, (self._game_shell + "\n") if self._game_shell else "", "")
            return (0, "", "")                         # mkdir
        def su(self, cmd, timeout=900):
            self.calls.append(("su", cmd))
            if "game_launcher" in cmd and self._game_su is not None:
                return (0, self._game_su + "\n", "")
            return (124, "", "timeout")                # su blocked
        def push(self, src, dst):
            self.calls.append(("push", str(src)))
            return self._push_ok

    def _run(self, fake):
        from cas import gui as G
        from unittest.mock import patch
        app = G.App.__new__(G.App); app.adb_bin = None
        with patch.object(G, "Adb", lambda serial=None, adb=None: fake):
            return app._detect_device_launchers("S1"), fake.calls

    def test_pushes_libroot_then_resolves_both(self):
        fake = self._FakeAdb()
        (game, home), calls = self._run(fake)
        self.assertEqual((game, home), ("com.handheld.launcher", "com.android.launcher3"))
        self.assertTrue(any(c[0] == "push" and c[1].endswith("lib-root.sh") for c in calls))  # pushed first
        self.assertTrue(any(c[0] == "shell" and "home_launcher" in c[1] for c in calls))       # home no-root

    def test_game_falls_back_to_shell_when_su_blocked(self):
        fake = self._FakeAdb(game_su=None, game_shell="com.handheld.launcher")  # su yields nothing
        (game, home), calls = self._run(fake)
        self.assertEqual(game, "com.handheld.launcher")                          # curated shell fallback hit
        self.assertTrue(any(c[0] == "su" and "game_launcher" in c[1] for c in calls))

    def test_push_failure_returns_none(self):
        fake = self._FakeAdb(push_ok=False)
        (game, home), _ = self._run(fake)
        self.assertEqual((game, home), (None, None))

    def test_no_serial_returns_none(self):
        from cas import gui as G
        app = G.App.__new__(G.App); app.adb_bin = None
        self.assertEqual(app._detect_device_launchers(""), (None, None))


class TestApkStoreDeploy(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("CAS_CONFIG", "CAS_PROFILES", "CAS_COMPANION_APK")}

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_split_manifest_apps(self):
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "p", apps=["com.captured"])                     # captured -> payload module exists
            pay = prof.payload
            pkgs = ["com.captured", "org.cocoon.app", "com.cfgonly"]
            axes = {"com.captured": (True, True), "org.cocoon.app": (True, False),
                    "com.cfgonly": (False, True)}
            payload, managed = PV._split_manifest_apps(pay, pkgs, axes)
            self.assertEqual(payload, ["com.captured"])
            self.assertEqual(managed, ["org.cocoon.app"])                 # apk-axis, no module; cfgonly excluded

    def test_install_apk_single_and_split(self):
        fr = FakeRunner(); adb = Adb(runner=fr)
        base = pathlib.Path("/x/base.apk")
        self.assertTrue(PV._install_apk(adb, "p", [base], log=lambda *a: None))
        # _install_apk passes str(Path); compare the OS-native rendering (Windows -> backslashes),
        # not a hardcoded POSIX literal, so this test is cross-platform.
        self.assertTrue(any(c[-1] == str(base) and "install" in c for c in fr.calls))
        fr2 = FakeRunner(); adb2 = Adb(runner=fr2)
        PV._install_apk(adb2, "p", [base, pathlib.Path("/x/split.apk")],
                        log=lambda *a: None)
        self.assertTrue(any("install-multiple" in c for c in fr2.calls))

    def test_install_store_app_pc_to_multiple_serials(self):
        # ad-hoc install: the store's CURRENT build is pushed to EACH serial via adb install
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.cocoon.app", "v1", content="apkbytes")
            runners = {}

            def mk_adb(s):
                fr = FakeRunner(); runners[s] = fr
                return Adb(serial=s, runner=fr)
            res = PV.install_store_app_pc(str(store), "org.cocoon.app", mk_adb, ["S1", "S2"],
                                          log=lambda *a: None)
            self.assertEqual(res, {"S1": True, "S2": True})
            for s in ("S1", "S2"):
                self.assertTrue(
                    any("install" in c and any("v1.apk" in x for x in c) for c in runners[s].calls),
                    f"expected install of v1.apk on {s}; calls={runners[s].cmds()}")

    def test_install_store_app_pc_missing_build_is_noop(self):
        # no current build for the pkg -> {} + a note, no adb call attempted
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"; store.mkdir()
            logs = []
            res = PV.install_store_app_pc(str(store), "org.absent.app",
                                          lambda s: Adb(serial=s, runner=FakeRunner()), ["S1"],
                                          log=lambda m: logs.append(m))
            self.assertEqual(res, {})
            self.assertTrue(any("no current build" in m for m in logs), f"logs={logs}")

    def test_install_store_app_pc_best_effort_on_failure(self):
        # S1's adb install fails (rc!=0); S2 succeeds -> S1 False, S2 True, BOTH still attempted
        class _FailInstall(FakeRunner):
            def __call__(self, args, input_text=None, timeout=900):
                self.calls.append(list(args))
                if "install" in args or "install-multiple" in args:
                    return 1, "", "INSTALL_FAILED"
                return 0, "", ""
        with tempfile.TemporaryDirectory() as t:
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.cocoon.app", "v1", content="apkbytes")
            runners = {}

            def mk_adb(s):
                fr = _FailInstall() if s == "S1" else FakeRunner()
                runners[s] = fr
                return Adb(serial=s, runner=fr)
            res = PV.install_store_app_pc(str(store), "org.cocoon.app", mk_adb, ["S1", "S2"],
                                          log=lambda *a: None)
            self.assertEqual(res, {"S1": False, "S2": True})
            self.assertTrue(any("install" in c for c in runners["S1"].calls))   # S1 was attempted...
            self.assertTrue(any("install" in c for c in runners["S2"].calls))   # ...and S2 too (not aborted)

    def test_provision_installs_managed_store_app(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            prof = _mk(t, "cocoon", apps=["org.es_de.frontend"])          # one captured app
            P.save_manifest(prof.manifest_path, ["org.es_de.frontend", "org.cocoon.app"],
                            {"settings": "on"}, header="# cocoon",
                            axes={"org.es_de.frontend": (True, True), "org.cocoon.app": (True, False)})
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.cocoon.app", "v1", content="apkbytes")
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            C.set_apk_store(str(store))
            fr = FakeRunner(model="Retroid Pocket 6"); adb = Adb(runner=fr)
            ok = PV.provision(adb, P.Profile(prof.path), log=lambda *a: None)
            self.assertTrue(ok, f"provision failed; calls={fr.cmds()}")
            self.assertTrue(any("install" in c and any("v1.apk" in x for x in c) for c in fr.calls),
                            f"expected managed-app install; calls={fr.cmds()}")


    def test_kit_apk_prefers_store_then_resolve_asset(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            os.environ["CAS_PROFILES"] = t       # isolate apk_store_dir (else it falls back to the REAL data/profiles/_apks)
            prof = _mk(t, "p", apps=["a"])
            store = pathlib.Path(t) / "store"
            appdir = pathlib.Path(t)
            (appdir / "data" / "Apps").mkdir(parents=True)
            (appdir / "data" / "Apps" / "Magisk.apk").write_text("m")
            self.assertEqual(PV._kit_apk(PV.MAGISK_PKG, prof, str(appdir), "data/Apps/Magisk.apk"),
                             appdir / "data" / "Apps" / "Magisk.apk")        # no store -> bundle fallback
            _seed_store(store, PV.MAGISK_PKG, "v30", content="x")
            C.set_apk_store(str(store))
            self.assertEqual(PV._kit_apk(PV.MAGISK_PKG, prof, str(appdir), "data/Apps/Magisk.apk").name,
                             "v30.apk")                                       # store build wins

    def test_install_companion_prefers_store_then_bundle(self):
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            os.environ["CAS_PROFILES"] = t       # isolate apk_store_dir (else it falls back to the REAL data/profiles/_apks)
            store = pathlib.Path(t) / "store"
            bundle = pathlib.Path(t) / "companion-bundle.apk"; bundle.write_text("b")
            os.environ["CAS_COMPANION_APK"] = str(bundle)
            fr = FakeRunner(); adb = Adb(runner=fr)
            PV.install_companion(adb, log=lambda *a: None)                    # no store -> bundle
            self.assertTrue(any("companion-bundle.apk" in x for c in fr.calls for x in c))
            _seed_store(store, PV.COMPANION_PKG, "v9", content="s")
            C.set_apk_store(str(store))
            fr2 = FakeRunner(); adb2 = Adb(runner=fr2)
            PV.install_companion(adb2, log=lambda *a: None)                   # store build wins
            self.assertTrue(any("v9.apk" in x for c in fr2.calls for x in c))

    def test_provision_managed_only_returns_false(self):
        """Fix 3: a manifest selecting ONLY a managed app (no captured payload) must abort with False."""
        from cas import config as C
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            # Profile has one captured app (org.es_de.frontend) in the golden payload, but the manifest
            # only lists a managed (store) app — no captured app ticked.
            prof = _mk(t, "p", apps=["org.es_de.frontend"])
            store = pathlib.Path(t) / "store"
            _seed_store(store, "org.cocoon.app", "v1")
            C.set_apk_store(str(store))
            P.save_manifest(prof.manifest_path, ["org.cocoon.app"],
                            {"settings": "on"}, header="# p",
                            axes={"org.cocoon.app": (True, False)})
            msgs = []
            fr = FakeRunner(model="Odin2 Mini")
            ok = PV.provision(Adb(runner=fr), P.Profile(prof.path), log=msgs.append, dry_push=True)
            self.assertFalse(ok, "provision must return False when manifest selects only managed apps")
            self.assertTrue(any("store-managed" in m for m in msgs),
                            f"expected managed-only abort message; got: {msgs}")

    def test_provision_push_aborts_immediately_on_cancel(self):
        """A cancelled Download must STOP at once: the payload-push retry loop must not keep retrying a
        push (3x with sleeps) once the operator has hit Cancel."""
        import threading
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            prof = _mk(t, "p", apps=["com.app"])           # one captured payload app
            ev = threading.Event(); ev.set()               # operator already cancelled
            fr = FakeRunner(push_ok=False)                 # every push fails (would normally retry 3x)
            ok = PV.provision(Adb(runner=fr, cancel=ev), P.Profile(prof.path), log=lambda *a: None)
            self.assertFalse(ok)
            pushes = [c for c in fr.calls if "push" in c]
            self.assertEqual(len(pushes), 1, f"cancel must abort the push without retrying; got {len(pushes)}")


class TestAppLabels(unittest.TestCase):
    """Friendly-name map: both PS2 package ids are recognised so neither shows as a raw package id."""

    def test_ps2_package_ids_are_labelled(self):
        from cas.gui import _app_label
        self.assertEqual(_app_label("xyz.aethersx2.android"), "AetherSX2  ·  PS2")
        self.assertEqual(_app_label("xyz.aethersx2.tturnip"), "NetherSX2  ·  PS2")


class TestAssignFirmware(unittest.TestCase):
    """assign_firmware must PERSIST the manual firmware override (it used to call a non-existent
    config.set_device_firmware, which threw mid-handler so nothing was saved)."""

    def test_assign_firmware_persists_manual_override(self):
        import types
        from unittest import mock
        from cas.gui import App
        from cas import firmware as FW
        saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES")}
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            os.environ["CAS_PROFILES"] = str(t)                  # keep history_dir/log_event off the NAS
            try:
                app = App.__new__(App)
                app.dev_tree = types.SimpleNamespace(selection=lambda: ["2ee078bd"])
                app.log = lambda m: None
                app.refresh_devices = lambda: None
                with mock.patch("cas.gui.messagebox.askyesno", return_value=True):
                    app.assign_firmware("ayn-m2", ["2ee078bd"])
                df = FW.get_device_firmware()
                self.assertIn("2ee078bd", df)
                self.assertEqual(df["2ee078bd"]["firmware_id"], "ayn-m2")
                self.assertTrue(df["2ee078bd"]["manual"])
            finally:
                for k, v in saved.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_unassign_firmware_clears_override(self):
        import types
        from unittest import mock
        from cas.gui import App
        from cas import firmware as FW
        saved = {k: os.environ.get(k) for k in ("CAS_CONFIG", "CAS_PROFILES")}
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cfg.json")
            os.environ["CAS_PROFILES"] = str(t)
            try:
                FW.set_device_firmware("2ee078bd", "ayn-m0", manual=True)   # a (wrong) override is in place
                app = App.__new__(App)
                app.dev_tree = types.SimpleNamespace(selection=lambda: ["2ee078bd"])
                app.log = lambda m: None
                app.refresh_devices = lambda: None
                with mock.patch("cas.gui.messagebox.askyesno", return_value=True):
                    app.unassign_firmware(["2ee078bd"])
                self.assertNotIn("2ee078bd", FW.get_device_firmware())       # override cleared
            finally:
                for k, v in saved.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class TestRunChainReport(unittest.TestCase):
    """Regression: a completed chain (e.g. Save) must hand _report a result it can consume, or done()
    crashes mid-way and leaves the UI wedged in the busy/loading state (buttons greyed, watch cursor)."""

    def _fake(self):
        import types
        logs = []
        return types.SimpleNamespace(log=logs.append), logs

    def test_chain_result_is_report_compatible(self):
        from cas.gui import App
        r = App._chain_result(["A", "B"], ["A"])          # A survived, B failed
        # canonical (status, detail) 2-tuples with the "ok" success token _report/retry recognise
        self.assertEqual(r["A"], ("ok", ""))
        self.assertEqual(r["B"], ("fail", ""))
        fake, logs = self._fake()
        App._report(fake, "Running Save on 1 device(s)", r)   # must NOT raise
        self.assertTrue(any("1 ok" in m for m in logs), f"expected a '1 ok' summary; got {logs}")
        self.assertTrue(any("1 failed" in m for m in logs), f"expected a '1 failed' summary; got {logs}")

    def test_report_tolerates_short_status_tuple(self):
        """Defense-in-depth: a malformed 1-tuple status must never crash _report (which runs inside
        done(); a crash there leaves the controls disabled)."""
        from cas.gui import App
        fake, logs = self._fake()
        App._report(fake, "X", {"S1": ("ok",), "S2": ("fail",)})   # 1-tuples, no detail
        self.assertTrue(any("1 ok" in m for m in logs), f"got {logs}")


class TestProfileLibraryLabel(unittest.TestCase):
    """The Profile 'Library:' status line must surface an unreachable library (e.g. an unplugged external
    drive) instead of silently showing it as OK."""

    def setUp(self):
        from cas import gui
        self.label = gui._profile_library_label

    def test_library_reachable_shows_ok(self):
        out = self.label("/mnt/ext/CAS Profiles", reachable=True)
        self.assertEqual(out, "Library: /mnt/ext/CAS Profiles   ✓")

    def test_library_unreachable_shows_unplugged(self):
        out = self.label("/mnt/ext/CAS Profiles", reachable=False)
        self.assertIn("✗", out)
        self.assertIn("/mnt/ext/CAS Profiles", out)
        self.assertIn("unplugged", out)


class ValidatePayloadAxes(unittest.TestCase):
    """_validate_payload must be axis-aware: an apk-only app (axes=apk) legitimately has NO data.tar,
    so demanding data.tar for it wrongly aborts the Download (regression: Steam Link failed with
    'payload missing apk/data' though its apk-only capture was complete)."""

    def _payload(self, tmp):
        pay = pathlib.Path(tmp) / "golden_root_payload"
        pay.mkdir(parents=True)
        (pay / "global.meta").write_text("golden_serial=9C33-6BBD\n")
        return pay

    def _apk_only(self, pay, pkg):
        (pay / pkg / "apk").mkdir(parents=True)
        (pay / pkg / "apk" / "base.apk").write_text("x")   # apk captured, NO data.tar (axes=apk)

    def _full(self, pay, pkg):
        (pay / pkg / "apk").mkdir(parents=True)
        (pay / pkg / "apk" / "base.apk").write_text("x")
        (pay / pkg / "data.tar").write_text("x")

    def test_apk_only_app_validates_without_data_tar(self):
        with tempfile.TemporaryDirectory() as t:
            pay = self._payload(t)
            self._apk_only(pay, "com.valvesoftware.steamlink")
            self._full(pay, "com.github.stenzek.duckstation")
            pkgs = ["com.valvesoftware.steamlink", "com.github.stenzek.duckstation"]
            axes = {"com.valvesoftware.steamlink": (True, False),        # apk-only
                    "com.github.stenzek.duckstation": (True, True)}      # apk + config
            logs = []
            self.assertTrue(PV._validate_payload(pay, pkgs, axes, logs.append), logs)

    def test_missing_apk_when_apk_axis_on_still_fails(self):
        with tempfile.TemporaryDirectory() as t:
            pay = self._payload(t)
            (pay / "com.brokenapk").mkdir()             # apk axis on but no apk/*.apk => truncated
            logs = []
            self.assertFalse(
                PV._validate_payload(pay, ["com.brokenapk"], {"com.brokenapk": (True, False)}, logs.append))
            self.assertIn("com.brokenapk", " ".join(logs))

    def test_missing_data_when_config_axis_on_still_fails(self):
        with tempfile.TemporaryDirectory() as t:
            pay = self._payload(t)
            self._apk_only(pay, "com.wantsconfig")      # config axis on but no data.tar => incomplete
            logs = []
            self.assertFalse(
                PV._validate_payload(pay, ["com.wantsconfig"], {"com.wantsconfig": (True, True)}, logs.append))
            self.assertIn("com.wantsconfig", " ".join(logs))


class TestLibWatch(unittest.TestCase):
    def test_lib_watch_action_edges(self):
        from cas.gui import _lib_watch_action as act
        # no change → None, regardless of busy
        self.assertIsNone(act(True, True, False))
        self.assertIsNone(act(True, True, True))
        self.assertIsNone(act(False, False, False))
        self.assertIsNone(act(False, False, True))
        # unreachable → reachable while idle → full reconnect
        self.assertEqual(act(False, True, False), "reconnect")
        # unreachable → reachable while a job runs → defer (retry next tick)
        self.assertEqual(act(False, True, True), "defer")
        # reachable → unreachable → relabel, regardless of busy
        self.assertEqual(act(True, False, False), "disconnect")
        self.assertEqual(act(True, False, True), "disconnect")

    def _watch_app(self, was, now, busy):
        from cas.gui import App
        app = App.__new__(App)                 # bypass Tk __init__
        app._lib_last_reachable = was
        app.busy = busy
        app._lib_reachable = lambda: now
        calls = []
        for name in ("refresh_profiles", "refresh_devices", "_update_lib_label"):
            setattr(app, name, lambda n=name: calls.append(n))
        app.log = lambda m: None
        after = []
        app.win = type("W", (), {"after": lambda self, ms, fn: after.append(ms)})()
        app._calls, app._after = calls, after
        return app

    def test_watch_reconnect_idle_full_refresh(self):
        app = self._watch_app(was=False, now=True, busy=False)
        app._lib_watch()
        self.assertTrue(app._lib_last_reachable)
        self.assertIn("refresh_profiles", app._calls)
        self.assertIn("refresh_devices", app._calls)
        self.assertEqual(app._after, [2000])           # rescheduled once

    def test_watch_reconnect_busy_defers(self):
        app = self._watch_app(was=False, now=True, busy=True)
        app._lib_watch()
        self.assertFalse(app._lib_last_reachable)      # baseline unchanged
        self.assertNotIn("refresh_profiles", app._calls)
        self.assertNotIn("refresh_devices", app._calls)
        self.assertEqual(app._after, [2000])           # still rescheduled

    def test_watch_disconnect_relabels_keeps_profiles(self):
        app = self._watch_app(was=True, now=False, busy=False)
        app._lib_watch()
        self.assertFalse(app._lib_last_reachable)
        self.assertIn("_update_lib_label", app._calls)
        self.assertNotIn("refresh_profiles", app._calls)   # selection preserved
        self.assertEqual(app._after, [2000])

    def test_watch_no_change_noop(self):
        app = self._watch_app(was=True, now=True, busy=False)
        app._lib_watch()
        self.assertEqual(app._calls, [])               # nothing but the reschedule
        self.assertEqual(app._after, [2000])


class SpecPackagingTest(unittest.TestCase):
    """Guards the PyInstaller spec so a frozen build actually bundles the `cas` package.

    Regression (v0.3.0, first release after the scripts/ reorg 4c03961): the entry shims
    moved into scripts/ while `cas/` stayed at the repo root. PyInstaller adds the ENTRY
    SCRIPT's own directory (scripts/) to its import search path, so with pathex=[] it could
    not import `cas` during analysis and froze a bundle MISSING the package -> the exe died
    at launch with `ModuleNotFoundError: No module named 'cas'`. The spec MUST put the repo
    root on `pathex` so `cas` resolves during analysis. The CI freeze step never runtime-tests
    the exe, so this static check is the regression guard.
    """

    def _analyze_spec(self):
        """exec scripts/cas.spec with PyInstaller's injected globals stubbed; return
        (repo_root, [{'pathex':[...], 'datas':[(src,dest),...]} per Analysis(...) call])."""
        import types

        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        spec_path = os.path.join(repo, "scripts", "cas.spec")

        captured = []

        class _Stub:
            # the spec reads attrs off Analysis results (a.pure, a.scripts, a.binaries, …);
            # return harmless empties for any attribute it touches.
            def __getattr__(self, _name):
                return []

        def _Analysis(scripts, pathex=None, datas=None, **kw):
            captured.append({"pathex": list(pathex or []), "datas": list(datas or [])})
            return _Stub()

        def _factory(*a, **k):
            return _Stub()

        # The spec does `from PyInstaller.utils.hooks import collect_all` at eval time.
        # PyInstaller is a BUILD-only dep (absent in the test env), so fake the module tree.
        fake_hooks = types.ModuleType("PyInstaller.utils.hooks")
        fake_hooks.collect_all = lambda _name: ([], [], [])
        fake_utils = types.ModuleType("PyInstaller.utils")
        fake_utils.hooks = fake_hooks
        fake_pyi = types.ModuleType("PyInstaller")
        fake_pyi.utils = fake_utils

        keys = ("PyInstaller", "PyInstaller.utils", "PyInstaller.utils.hooks")
        saved = {k: sys.modules.get(k) for k in keys}
        sys.modules.update(dict(zip(keys, (fake_pyi, fake_utils, fake_hooks))))
        try:
            src = pathlib.Path(spec_path).read_text()
            ns = {
                # PyInstaller injects SPECPATH (abs dir of the .spec) + the build classes.
                "SPECPATH": os.path.join(repo, "scripts"),
                "Analysis": _Analysis,
                "PYZ": _factory, "EXE": _factory, "COLLECT": _factory, "BUNDLE": _factory,
                "__file__": spec_path,
            }
            exec(compile(src, spec_path, "exec"), ns)
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        return repo, captured

    def test_spec_pathex_includes_repo_root(self):
        repo, captured = self._analyze_spec()
        # sanity: the package we must bundle really lives at the repo root
        self.assertTrue(os.path.isdir(os.path.join(repo, "cas")))
        self.assertTrue(captured, "spec defined no Analysis() — did the spec change shape?")
        repo_abs = os.path.abspath(repo)
        for rec in captured:
            resolved = [os.path.abspath(p) for p in rec["pathex"]]
            self.assertIn(repo_abs, resolved,
                          "cas.spec Analysis(pathex=...) must include the repo root so "
                          "PyInstaller can import `cas` during analysis; otherwise the "
                          "frozen build ships without it (ModuleNotFoundError: No module "
                          "named 'cas' at launch).")

    # Every read-only resource the runtime loads from BUNDLE/ (= sys._MEIPASS when frozen). If any
    # is absent from the spec's datas it silently vanishes from the frozen bundle and the feature
    # that reads it dies at RUNTIME (e.g. magisk-patch missing -> "could not push the Magisk patch
    # toolkit", the unit never roots/boots). Keep in lock-step with the `BUNDLE / ...` refs in
    # cas/provision.py + cas/gui.py.
    REQUIRED_BUNDLED = [
        "provision/root/restore.sh",
        "provision/root/capture.sh",
        "provision/root/lib-root.sh",
        "provision/root/scrub.sh",
        "provision/root/grant-persist.sh",
        "provision/root/magisk-patch/boot_patch.sh",   # sourced by the on-device patch
        "provision/root/magisk-patch/util_functions.sh",
        "provision/root/magisk-patch/magiskboot",       # the aarch64 patcher binary
        "provision/root/magisk-patch/magiskinit",
        "provision/root/magisk-patch/magisk",
        "provision/root/magisk-patch/init-ld",
        "provision/root/magisk-patch/stub.apk",          # boot_patch.sh compresses this into the ramdisk
        "assets/cas-window.png",
    ]

    def test_spec_bundles_every_required_runtime_resource(self):
        import glob
        repo, captured = self._analyze_spec()
        self.assertTrue(captured, "spec defined no Analysis() — did the spec change shape?")
        # Expand every datas src (globs included, mirroring PyInstaller) to the set of real files it
        # ships, as repo-relative POSIX paths. glob only matches files that EXIST — so this also
        # guards that the resource is present in a clean checkout (i.e. committed, not gitignored).
        covered = set()
        esc_repo = glob.escape(repo)   # this bench's path has '[07]' — escape it so glob treats the
                                       # dir literally while the trailing '*' in a src stays a wildcard.
        for src, _dest in captured[0]["datas"]:
            pattern = esc_repo + src[len(repo):] if src.startswith(repo) else glob.escape(src)
            for m in glob.glob(pattern):
                if os.path.isfile(m):
                    rel = os.path.relpath(os.path.abspath(m), repo).replace(os.sep, "/")
                    covered.add(rel)
        missing = [r for r in self.REQUIRED_BUNDLED if r not in covered]
        self.assertEqual(missing, [],
                         "cas.spec datas does not bundle (or the file is missing/gitignored from a "
                         f"clean checkout): {missing}. These are read from BUNDLE/ at runtime; if not "
                         "frozen in, the feature dies on the operator's machine.")
        # Both exes must ship the SAME datas (COLLECT dedups); guard they don't drift apart.
        for rec in captured[1:]:
            self.assertEqual(rec["datas"], captured[0]["datas"],
                             "both Analysis() calls must pass identical datas")


class AdbLocalPathTest(unittest.TestCase):
    """adb push/pull must hand adb the LOCAL (PC-side) path with forward slashes. On Windows a
    backslash DIRECTORY path makes `adb push <dir>` report success but transfer 0 files — which is
    why the multi-file golden PAYLOAD push landed nothing and Download died with "no APK in payload"
    on the frozen Windows build, while the SAME profile pushed 1.3 GB fine on Linux (and single-file
    Magisk pushes rooted fine even on Windows). Regression guard for that 0-byte Windows Download."""

    def _rec(self):
        calls = []

        def runner(args, input_text=None, timeout=900):
            calls.append(list(args))
            return 0, "", ""
        return calls, runner

    def test_push_normalizes_backslash_local_src(self):
        from cas.adb import Adb
        calls, runner = self._rec()
        Adb(serial="X", runner=runner).push(
            r"D:\CAS Profiles\prof\golden_root_payload\com.x", "/data/local/tmp/cas/payload/")
        push = next(c for c in calls if "push" in c)
        local = push[push.index("push") + 1]
        self.assertEqual(local, "D:/CAS Profiles/prof/golden_root_payload/com.x")
        self.assertNotIn("\\", local)
        # the DEVICE path is untouched (already POSIX)
        self.assertEqual(push[-1], "/data/local/tmp/cas/payload/")

    def test_pull_normalizes_backslash_local_dst(self):
        from cas.adb import Adb
        calls, runner = self._rec()
        Adb(serial="X", runner=runner).pull("/data/local/tmp/cas/x", r"D:\CAS Profiles\out\x")
        pull = next(c for c in calls if "pull" in c)
        self.assertEqual(pull[-1], "D:/CAS Profiles/out/x")


class FastbootMissingHelpTest(unittest.TestCase):
    """When a unit reboots to the bootloader but never shows in `fastboot devices`, the operator gets
    OS-aware, actionable guidance — on Windows the REAL cause (missing bootloader USB driver) + the
    one-time fix, not a dead-end 'Aborting'. This is the fastboot-flash step of ⓪ Root."""

    def test_windows_points_at_the_bootloader_driver(self):
        from unittest import mock
        from cas import provision as PV
        with mock.patch.object(PV.os, "name", "nt"):
            msg = PV.fastboot_missing_help()
        self.assertIn("BOOTLOADER USB DRIVER", msg)
        self.assertIn("setup-windows.bat", msg)      # the shipped helper
        self.assertIn("WinUSB", msg)                 # the driver to install
        self.assertNotIn("android-udev", msg)        # that's the POSIX branch, not here

    def test_posix_is_cable_mode_guidance_not_driver(self):
        from unittest import mock
        from cas import provision as PV
        with mock.patch.object(PV.os, "name", "posix"):
            msg = PV.fastboot_missing_help()
        self.assertIn("did not enter fastboot", msg)
        self.assertNotIn("BOOTLOADER USB DRIVER", msg)


class WindowsDriverKitTest(unittest.TestCase):
    """The Windows USB-driver setup (setup-windows.bat -> drivers\\install-drivers.ps1) closes the flash-
    interface driver gap that blocks ⓪ Root / ③ Seal on Windows, ONE-TIME + fleet-wide (no Zadig). These
    static checks guard that the shipped assets exist, target the right USB ids, and — the hard lesson from
    past releases — are actually copied INTO the Windows kit by CI (an asset that isn't packaged silently
    disables the feature on the operator's rig)."""

    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def _read(self, *parts):
        p = os.path.join(self.repo, *parts)
        self.assertTrue(os.path.isfile(p), f"missing shipped asset: {os.path.join(*parts)}")
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_driver_assets_exist(self):
        for parts in (
            ("scripts", "setup-windows.bat"),
            ("scripts", "drivers", "install-drivers.ps1"),
            ("scripts", "drivers", "README.md"),
            ("scripts", "drivers", "fallback", "fastboot", "cas-fastboot.inf"),
            ("scripts", "drivers", "fallback", "edl", "cas-edl-9008.inf"),
        ):
            self._read(*parts)   # raises if absent

    def test_fastboot_inf_targets_standard_id_class_and_android_guid(self):
        inf = self._read("scripts", "drivers", "fallback", "fastboot", "cas-fastboot.inf")
        self.assertIn("USB\\VID_18D1&PID_D00D", inf)                       # standard Android fastboot id
        self.assertIn("USB\\Class_ff&SubClass_42&Prot_03", inf)           # fastboot interface class (brand-agnostic)
        # the ADB/fastboot device-interface GUID fastboot.exe enumerates — the bit generic Zadig WinUSB omits
        self.assertIn("{F72FE0D4-CAE1-11D1-B3B3-00E01F809FEB}", inf)
        self.assertIn("Needs   = WINUSB.NT", inf)                          # binds the in-box WinUSB driver

    def test_edl_inf_targets_qualcomm_9008(self):
        inf = self._read("scripts", "drivers", "fallback", "edl", "cas-edl-9008.inf")
        self.assertIn("USB\\VID_05C6&PID_9008", inf)                      # Qualcomm EDL 9008 (fixed across all EDL units)
        self.assertIn("usbser.sys", inf)                                  # in-box serial driver -> COM port

    def test_setup_bat_elevates_and_runs_the_installer(self):
        bat = self._read("scripts", "setup-windows.bat")
        self.assertIn("net session", bat)                                 # admin check
        self.assertIn("RunAs", bat)                                       # self-elevation
        self.assertIn("drivers\\install-drivers.ps1", bat)                # delegates to the installer

    def test_installer_publishes_to_driver_store(self):
        ps1 = self._read("scripts", "drivers", "install-drivers.ps1")
        self.assertIn("/add-driver", ps1)                                 # pnputil driver-store publish...
        self.assertIn("/install", ps1)                                    # ...applied to connected + future units

    def test_windows_consumed_scripts_are_pure_ascii(self):
        # Windows PowerShell 5.1 reads a no-BOM .ps1 as the ANSI code page (CP1252), NOT UTF-8. A UTF-8
        # em-dash (U+2014 = bytes E2 80 94) then decodes as three chars ending in 0x94 = U+201D, a curly
        # double-quote that PowerShell treats as a string delimiter -> quotes unbalance and the whole
        # script dies with "missing terminator". cmd.exe (.bat) and the INF parser are ANSI too. So every
        # Windows-EXECUTED script must be pure ASCII. (README.md is documentation, not executed -> exempt.)
        # Regression for the bench that couldn't run setup-windows.bat: install-drivers.ps1 had 9 em-dashes.
        # GLOB every Windows-executed script under scripts/ (.ps1/.bat/.inf) so a NEW one is auto-guarded
        # without editing this list (install-edl-host-tools.ps1 was added this way).
        scripts = os.path.join(self.repo, "scripts")
        wanted = []
        for dirpath, _dirs, files in os.walk(scripts):
            for name in files:
                if os.path.splitext(name)[1].lower() in (".ps1", ".bat", ".inf"):
                    wanted.append(os.path.relpath(os.path.join(dirpath, name), self.repo))
        self.assertTrue(wanted, "no Windows-consumed scripts found under scripts/ - glob is broken")
        for rel in sorted(wanted):
            parts = rel.split(os.sep)
            p = os.path.join(self.repo, rel)
            with open(p, "rb") as f:
                raw = f.read()
            bad, line, col = [], 1, 0
            for byte in raw:
                if byte == 0x0A:
                    line, col = line + 1, 0
                    continue
                col += 1
                if byte > 0x7F:
                    bad.append((line, col, hex(byte)))
            self.assertEqual(
                bad, [],
                f"{os.path.join(*parts)} has non-ASCII bytes (breaks Windows PowerShell/cmd/INF parsing); "
                f"first few (line,col,byte): {bad[:5]}",
            )

    def test_ci_packages_the_drivers_tree_into_the_windows_kit(self):
        # Regression guard (v0.3.x saga): a Windows helper the CI forgot to copy = feature silently absent.
        yml = self._read(".github", "workflows", "build.yml")
        self.assertIn("cp -r scripts/drivers dist/cas/drivers", yml)
        self.assertIn("drivers/install-drivers.ps1", yml)                 # the fail-loud presence check

    def test_build_ships_the_overlay_boot_grant_payload(self):
        # The overlay.d boot-grant (cas.provision.OVERLAY_DIR) is pushed to the device at Root time, so it
        # MUST be frozen into the bundle AND presence-checked in CI. This repo bundles provision/root/ file-
        # by-file, so a new file that is not added ships ABSENT: OVERLAY_DIR.is_dir() is False under _MEIPASS
        # -> inject silently skipped -> the feature is inert in the release while source-run tests stay green.
        spec = self._read("scripts", "cas.spec")
        self.assertIn("provision/root/overlay", spec)                 # cas.spec bundles the payload
        yml = self._read(".github", "workflows", "build.yml")
        self.assertIn("provision/root/overlay/cas-grant.sh", yml)     # CI frozen-bundle presence guard
        self.assertIn("provision/root/overlay/init.cas-grant.rc", yml)


class TestOverlayBootGrant(unittest.TestCase):
    def setUp(self):
        # CAS_CONFIG isolation: test_bake_boot_grant_default_on points it at a scratch dir and cleans up
        # with a bare del (assuming it was previously unset) — restore whatever it actually was.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    def _overlay(self, name):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return pathlib.Path(repo) / "provision" / "root" / "overlay" / name

    def test_overlay_files_exist_and_are_lf_only(self):
        for name in ("cas-grant.sh", "init.cas-grant.rc"):
            raw = self._overlay(name).read_bytes()
            self.assertNotIn(b"\r", raw, f"{name} must be LF-only (device init/sh consumed)")
            self.assertTrue(raw.endswith(b"\n"), f"{name} must end with a newline")
            self.assertEqual([b for b in raw if b > 0x7F], [],
                             f"{name} must be pure ASCII (non-ASCII breaks device init/sh parsing)")

    def test_cas_grant_writes_the_shell_allow_policy(self):
        sh = self._overlay("cas-grant.sh").read_text()
        # exact policy rows grant-persist.sh writes: shell uid 2000 = allow, adb+apps root
        self.assertIn("policies (uid,policy,until,logging,notification) VALUES(2000,2,0,0,0)", sh)
        self.assertIn("settings (key,value) VALUES('root_access',3)", sh)
        self.assertIn("/data/adb/magisk/magisk", sh)              # applet resolved off-PATH
        self.assertIn("/data/local/tmp/cas_boot_grant.done", sh)  # bench diagnostic marker
        # Bounded retry: a COUNTED loop with a numeric default cap, never an open loop. (The dynamic
        # proof that it actually exhausts is tests/test_cas_grant.sh's daemon-not-ready scenario.)
        self.assertRegex(sh, r'while \[ "\$i" -lt ')             # counted, not `while true`/`while :`
        self.assertRegex(sh, r"CAS_GRANT_TRIES:-\d+")            # numeric default cap on the retry
        self.assertNotRegex(sh, r"while\s+(true|:)")

    def test_rc_starts_the_service_as_root_at_boot_completed(self):
        rc = self._overlay("init.cas-grant.rc").read_text()
        self.assertIn("service cas_grant /system/bin/sh", rc)
        # magiskinit moves overlay.d/ contents to '/', so the script lands at /cas-grant.sh (NOT
        # /overlay.d/cas-grant.sh — the old path was a no-op that never ran the grant). The service must
        # exec the root-relocated path; it may also cover the un-relocated one for robustness.
        self.assertIn("/cas-grant.sh", rc)
        self.assertNotRegex(rc, r"exec\s+/system/bin/sh\s+/overlay\.d/cas-grant\.sh\b")
        self.assertIn("user root", rc)
        self.assertIn("seclabel u:r:magisk:s0", rc)
        self.assertIn("oneshot", rc)
        self.assertIn("on property:sys.boot_completed=1", rc)
        self.assertIn("start cas_grant", rc)

    def test_bake_boot_grant_default_on(self):
        from cas import config
        with tempfile.TemporaryDirectory() as d:
            os.environ["CAS_CONFIG"] = os.path.join(d, "cas-config.json")  # no file -> default
            try:
                self.assertTrue(config.bake_boot_grant())
            finally:
                del os.environ["CAS_CONFIG"]


class TestRealConfigNeverWritten(unittest.TestCase):
    """Regression guard for Finding 2: tests/test_cas.py::TestProvision::test_provision_never_pushes_a_directory
    used to call PV.provision() on the real (non-dry) push path with CAS_CONFIG unset, so
    config.record_download() wrote straight through to the operator's REAL repo-root cas-config.json
    (gitignored — which is exactly why nobody noticed: `git status` stays clean while the file quietly
    fills with junk download_stats rows). This proves the invariant holds: exercising every config
    writer named in the audit, under CAS_CONFIG isolation, must leave the real file byte-identical.
    Skips cleanly on a fresh checkout that has no real config file yet (nothing to protect)."""

    def _real_config_path(self):
        from cas import APPDIR
        return pathlib.Path(APPDIR) / "cas-config.json"

    def test_writing_under_isolation_never_touches_the_real_config(self):
        real = self._real_config_path()
        if not real.exists():
            self.skipTest("no real cas-config.json in this checkout (fresh clone) — nothing to protect yet")
        import hashlib
        before_hash = hashlib.sha256(real.read_bytes()).hexdigest()
        before_mtime = real.stat().st_mtime_ns

        saved = os.environ.get("CAS_CONFIG")
        with tempfile.TemporaryDirectory() as t:
            os.environ["CAS_CONFIG"] = str(pathlib.Path(t) / "cas-config.json")
            try:
                from cas import config as C
                from cas import firmware as FW
                # Exercise every writer named in the Finding 2 audit — under isolation, NONE may reach
                # the real file.
                C.record_download(1024, 1.0, profile="regression-guard")
                C.set_device_profile("REGRESSION-SERIAL", "regression-profile")
                C.set_always_install_pkgs(["com.regression.guard"])
                C.set_library("/tmp/regression-guard-lib")
                C.set_log_dir("/tmp/regression-guard-log")
                C.set_es_media_src("/tmp/regression-guard-media")
                C.set_firmware_dir("/tmp/regression-guard-fw")
                C.set_apk_store("/tmp/regression-guard-apks")
                FW.set_device_firmware("REGRESSION-SERIAL", "regression-fw", manual=True)
            finally:
                if saved is None:
                    os.environ.pop("CAS_CONFIG", None)
                else:
                    os.environ["CAS_CONFIG"] = saved

        after_hash = hashlib.sha256(real.read_bytes()).hexdigest()
        after_mtime = real.stat().st_mtime_ns
        self.assertEqual(before_hash, after_hash,
                         "a config writer touched the REAL cas-config.json even though CAS_CONFIG "
                         "was isolated — the never-write-the-real-config invariant is broken")
        self.assertEqual(before_mtime, after_mtime,
                         "the REAL cas-config.json's mtime changed even though CAS_CONFIG was isolated")


if __name__ == "__main__":
    unittest.main()
