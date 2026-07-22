# tests/test_seal_capture.py
import os
import struct
import sys
import pathlib
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import provision as PV
from cas import initboot_store as IBS

FP = "qti/kalama/kalama:13/TKQ1.231222.001/eng.RP6.20260119.170007:user/release-keys"


def _fake_adb(slot="_a", su_rc=0, pull_bytes=b"ANDROID!" + b"\x00" * 1024, fp=FP, boot_partition="init_boot"):
    adb = mock.Mock()
    adb.slot_suffix.return_value = slot
    adb.boot_partition.return_value = boot_partition
    adb.getprop.return_value = fp

    # capture_factory_init_boot now refuses to read the inactive slot unless it can PROVE no update is
    # staged (a staged payload writes the target slot, which is how the AIR X store got poisoned).
    # Report a clean IDLE update_engine so these tests exercise the capture path itself.
    def _su(cmd, timeout=900):
        if "update_engine_client" in cmd:
            return (0, "CURRENT_OP=UPDATE_STATUS_IDLE", "")
        return (su_rc, "", "")

    adb.su.side_effect = _su

    def _pull(src, dst):
        if pull_bytes is None:
            return False
        pathlib.Path(dst).write_bytes(pull_bytes)
        return True

    adb.pull.side_effect = _pull
    return adb


def _dd_cmd(adb):
    """The dd command capture issued. Found by CONTENT, not call index: the staged-update probe runs
    before it, so position is not a stable contract."""
    for call in adb.su.call_args_list:
        cmd = call[0][0]
        if cmd.startswith("dd "):
            return cmd
    raise AssertionError(f"no dd command issued; su calls: {[c[0][0] for c in adb.su.call_args_list]}")


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = pathlib.Path(self.tmp) / "_init_boot_factory"

    def test_captures_inactive_slot_b_when_active_a(self):
        adb = _fake_adb(slot="_a")
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertTrue(ok)
        self.assertTrue(IBS.has(self.store, FP))
        # dumped the INACTIVE slot (_b) since active is _a.
        ddcmd = _dd_cmd(adb)
        self.assertIn("init_boot_b", ddcmd)

    def test_captures_boot_partition_on_pre_init_boot_unit(self):
        # A unit that launched pre-Android-13 keeps its ramdisk in 'boot', not 'init_boot' — the capture
        # must dd from THIS unit's real boot partition (adb.boot_partition()), not a hardcoded 'init_boot'.
        adb = _fake_adb(slot="_a", boot_partition="boot")
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertTrue(ok)
        ddcmd = _dd_cmd(adb)
        self.assertIn("boot_b", ddcmd)
        self.assertNotIn("init_boot_b", ddcmd)

    def test_rejects_empty_inactive_slot(self):
        adb = _fake_adb(pull_bytes=b"\x00" * 4096)
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertFalse(ok)
        self.assertFalse(IBS.has(self.store, FP))

    def test_rejects_magisk_patched_image(self):
        adb = _fake_adb(pull_bytes=b"ANDROID!....MAGISKINIT....")
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertFalse(ok)
        self.assertFalse(IBS.has(self.store, FP))

    def test_non_fatal_on_pull_failure(self):
        adb = _fake_adb(pull_bytes=None)
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertFalse(ok)  # returns False, does not raise

    def test_skips_when_no_inactive_slot(self):
        adb = _fake_adb(slot="")  # A-only device: no inactive slot
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertFalse(ok)
        adb.su.assert_not_called()

    def test_non_fatal_on_store_write_error(self):
        # A valid, non-Magisk image reaches the store write — but the write itself blows up
        # (disk full / permission denied on the store). Capture must swallow it, log a skip,
        # and return False — never propagate. root() succeeds regardless is the whole point.
        # The REAL store.put() runs (not mocked) — only its atomic os.replace() is made to fail, so this
        # also proves put()'s own failure path (raise, no dest left behind — see test_initboot_store.py's
        # TestAtomicWrite) is actually non-fatal one layer up, at the capture call site.
        adb = _fake_adb()
        with mock.patch.object(IBS.os, "replace", side_effect=OSError("disk full")):
            ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertFalse(ok)
        self.assertFalse(IBS.has(self.store, FP))


class TestSealResolve(unittest.TestCase):
    def test_prefers_capture_over_library(self):
        logs = []
        out = PV.resolve_seal_stock("/lib/kit_init_boot.img", "/store/cap.img", FP, log=logs.append)
        self.assertEqual(out, "/store/cap.img")
        self.assertTrue(any("captured factory init_boot" in m for m in logs))

    def test_falls_back_to_library_with_warning(self):
        logs = []
        out = PV.resolve_seal_stock("/lib/kit_init_boot.img", None, FP, log=logs.append)
        self.assertEqual(out, "/lib/kit_init_boot.img")
        self.assertTrue(any("OTA may fail" in m for m in logs))


class TestRootCaptureWiring(unittest.TestCase):
    def test_root_calls_capture_on_success(self):
        calls = []

        def fake_capture(adb, store_root, log=print):
            calls.append(store_root)
            return True

        adb = mock.Mock()
        # Drive root() straight to its success return: booted + granted, no boot-grant bake.
        # is_root() is checked TWICE: once up front ("already rooted?" — must be False so root()
        # doesn't short-circuit before ever flashing/capturing) and once post-boot as the granted
        # check (bake_boot_grant is patched False below, so `granted = adb.is_root()`) — must be True.
        adb.wait_boot.return_value = True
        adb.is_root.side_effect = [False, True]
        adb.getprop.return_value = "init_boot"  # any nonempty; kernel-size guard is bypassed below

        with mock.patch.object(PV, "capture_factory_init_boot", side_effect=fake_capture), \
             mock.patch.object(PV, "_await_boot_grant", return_value=True), \
             mock.patch.object(PV, "patch_init_boot_on_device", return_value=True), \
             mock.patch.object(PV, "_img_kernel_size", return_value=0), \
             mock.patch.object(PV.pathlib.Path, "exists", return_value=True), \
             mock.patch("cas.config.bake_boot_grant", return_value=False), \
             mock.patch("cas.config.auto_grant_shell", return_value=False):
            adb.boot_flash_target.return_value = "init_boot_a"
            adb.is_golden.return_value = False
            ok = PV.root(adb, mock.Mock(), "/lib/stock_init_boot.img",
                         magisk_apk=None, log=lambda *a: None,
                         flasher=lambda *a, **k: True, capture_store="/store")
        self.assertTrue(ok)
        self.assertEqual(calls, ["/store"])

    def test_root_does_not_capture_when_no_store(self):
        adb = mock.Mock()
        adb.wait_boot.return_value = True
        adb.is_root.side_effect = [False, True]  # see comment above: unrooted, then granted post-boot
        adb.getprop.return_value = "init_boot"

        with mock.patch.object(PV, "capture_factory_init_boot") as cap, \
             mock.patch.object(PV, "_await_boot_grant", return_value=True), \
             mock.patch.object(PV, "patch_init_boot_on_device", return_value=True), \
             mock.patch.object(PV, "_img_kernel_size", return_value=0), \
             mock.patch.object(PV.pathlib.Path, "exists", return_value=True), \
             mock.patch("cas.config.bake_boot_grant", return_value=False), \
             mock.patch("cas.config.auto_grant_shell", return_value=False):
            adb.boot_flash_target.return_value = "init_boot_a"
            adb.is_golden.return_value = False
            ok = PV.root(adb, mock.Mock(), "/lib/stock_init_boot.img",
                         magisk_apk=None, log=lambda *a: None,
                         flasher=lambda *a, **k: True)
        self.assertTrue(ok)
        cap.assert_not_called()

    def test_root_calls_capture_on_auto_grant_success(self):
        """The SECOND success return point: the boot-grant check comes back False (granted=False),
        so root() falls to the auto-grant branch (`if _cfg.auto_grant_shell(): ... if grant_shell_root(...):
        return True`) — that branch has its OWN `if capture_store: capture_factory_init_boot(...)` call,
        untested by test_root_calls_capture_on_success above (which only ever reaches the FIRST
        `if granted:` block). Drive root() down THIS path and confirm capture still fires."""
        calls = []

        def fake_capture(adb, store_root, log=print):
            calls.append(store_root)
            return True

        adb = mock.Mock()
        adb.wait_boot.return_value = True
        # is_root() is checked TWICE before the auto-grant branch: once up front ("already rooted?" —
        # must be False so root() doesn't short-circuit) and once as the granted check (bake_boot_grant
        # patched False below, so `granted = adb.is_root()`) — must ALSO be False here so control falls
        # through past `if granted:` into the `if _cfg.auto_grant_shell():` branch.
        adb.is_root.side_effect = [False, False]
        adb.getprop.return_value = "init_boot"  # any nonempty; kernel-size guard is bypassed below

        with mock.patch.object(PV, "capture_factory_init_boot", side_effect=fake_capture), \
             mock.patch.object(PV, "_await_boot_grant", return_value=True), \
             mock.patch.object(PV, "patch_init_boot_on_device", return_value=True), \
             mock.patch.object(PV, "_img_kernel_size", return_value=0), \
             mock.patch.object(PV.pathlib.Path, "exists", return_value=True), \
             mock.patch.object(PV, "grant_shell_root", return_value=True), \
             mock.patch("cas.config.bake_boot_grant", return_value=False), \
             mock.patch("cas.config.auto_grant_shell", return_value=True):
            adb.boot_flash_target.return_value = "init_boot_a"
            adb.is_golden.return_value = False
            ok = PV.root(adb, mock.Mock(), "/lib/stock_init_boot.img",
                         magisk_apk=None, log=lambda *a: None,
                         flasher=lambda *a, **k: True, capture_store="/store")
        self.assertTrue(ok)
        self.assertEqual(calls, ["/store"])


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_initboot_store import make_boot_img, PATCHED_RAMDISK, CLEAN_RAMDISK   # noqa: E402


def _seal_adb(rooted=True, fp=FP, target="boot_a"):
    adb = mock.Mock()
    adb.is_root.return_value = rooted
    adb.is_golden.return_value = False
    adb.boot_flash_target.return_value = target
    adb.slot_suffix.return_value = "_a"
    adb.getprop.return_value = fp
    adb.push.return_value = True
    adb.su.return_value = (0, "", "")
    adb.su_stream.return_value = 0
    adb.shell.return_value = (0, "", "")
    return adb


class TestSealRefusesRootedUnrootImage(unittest.TestCase):
    """③ Lock un-roots by flashing a 'stock' image. On 2026-07-20 the RP5's library image was itself
    Magisk-patched (the Banners FW189 OC mod), so every Lock re-rooted the unit and failed with
    'still ROOTED after stock flash'. seal() must inspect the image BEFORE flashing it — an image
    carrying Magisk is by definition not stock and can never un-root anything."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.flashed = []

    def _img(self, ramdisk, name="stock.img"):
        p = pathlib.Path(self.tmp) / name
        p.write_bytes(make_boot_img(ramdisk, "gzip"))
        return str(p)

    def _flasher(self, adb, target, image, log):
        self.flashed.append((target, image))
        return True

    def _seal(self, stock, **kw):
        msgs = []
        ok = PV.seal(_seal_adb(), mock.Mock(), stock, log=msgs.append, wait=False,
                     flasher=self._flasher, **kw)
        return ok, "\n".join(msgs)

    def test_refuses_to_flash_a_magisk_patched_image(self):
        ok, log = self._seal(self._img(PATCHED_RAMDISK))
        self.assertFalse(ok)
        self.assertEqual(self.flashed, [], "must not flash a rooted image at all")
        self.assertIn("magisk", log.lower())

    def test_force_does_not_bypass_the_rooted_image_refusal(self):
        # force= exists to override a MODEL mismatch. A rooted un-root image is a physical impossibility,
        # not an operator judgement call, so force must not get past it.
        ok, _ = self._seal(self._img(PATCHED_RAMDISK), force=True)
        self.assertFalse(ok)
        self.assertEqual(self.flashed, [])

    def test_clean_image_still_seals(self):
        ok, _ = self._seal(self._img(CLEAN_RAMDISK))
        self.assertTrue(ok)
        self.assertEqual(len(self.flashed), 1)

    def test_unreadable_image_warns_but_proceeds(self):
        # UNKNOWN must not block the fleet: the post-flash is_root() check still backstops it, and
        # refusing here would strand every unit whose ramdisk codec we cannot read.
        p = pathlib.Path(self.tmp) / "opaque.img"
        p.write_bytes(b"\x00" * 4096)
        ok, log = self._seal(str(p))
        self.assertTrue(ok)
        self.assertEqual(len(self.flashed), 1)
        self.assertIn("could not verify", log.lower())


class TestShipsRootedOptIn(unittest.TestCase):
    """Some builds are DELIBERATELY rooted — the RP5's 905MHz overclock kernel only ships as the
    'Banners root+overclock' image, and Donald chose to keep the OC and ship those units rooted. For
    those, ③ Lock must still do the retail lockdown (hide dev options, kill adb) but SKIP the un-root
    flash, which could only ever re-root the unit. This is opt-in per firmware build: without the
    declaration a Magisk-patched image is still refused, so it can never happen by accident."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.flashed = []
        self.p = pathlib.Path(self.tmp) / "oc.img"
        self.p.write_bytes(make_boot_img(PATCHED_RAMDISK, "gzip"))

    def _flasher(self, adb, target, image, log):
        self.flashed.append(target)
        return True

    def _seal(self, **kw):
        msgs = []
        adb = _seal_adb()
        ok = PV.seal(adb, mock.Mock(), str(self.p), log=msgs.append, wait=False,
                     flasher=self._flasher, **kw)
        return ok, "\n".join(msgs), adb

    def test_without_optin_a_rooted_image_is_still_refused(self):
        ok, _, _ = self._seal()
        self.assertFalse(ok)

    def test_with_optin_seals_without_flashing(self):
        ok, log, adb = self._seal(allow_rooted_image=True)
        self.assertTrue(ok)
        self.assertEqual(self.flashed, [], "must not flash a rooted image even when opted in")
        self.assertIn("rooted", log.lower())

    def test_with_optin_still_runs_the_retail_lockdown(self):
        ok, _, adb = self._seal(allow_rooted_image=True)
        self.assertTrue(ok)
        shell = "\n".join(str(c) for c in adb.shell.call_args_list)
        self.assertIn("development_settings_enabled 0", shell)
        self.assertIn("adb_enabled 0", shell)          # USB debugging off — the unit really is sealed

    def test_optin_does_not_apply_to_a_clean_image(self):
        # A clean image must still be flashed normally — the opt-in is a narrow exemption for a
        # deliberately-rooted build, not a blanket 'skip the un-root flash' switch.
        clean = pathlib.Path(self.tmp) / "clean.img"
        clean.write_bytes(make_boot_img(CLEAN_RAMDISK, "gzip"))
        msgs = []
        ok = PV.seal(_seal_adb(), mock.Mock(), str(clean), log=msgs.append, wait=False,
                     flasher=self._flasher, allow_rooted_image=True)
        self.assertTrue(ok)
        self.assertEqual(self.flashed, ["boot_a"])


class TestSealPartitionTypeGuard(unittest.TestCase):
    """root() refuses a kernel-less init_boot aimed at a `boot` partition (it strips the kernel and
    bootloops the unit — the RP5 brick). seal() flashes the same class of image and had no such guard."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.flashed = []

    def _write(self, kernel_size):
        p = pathlib.Path(self.tmp) / "img.img"
        hdr = bytearray(4096)
        hdr[0:8] = b"ANDROID!"
        struct.pack_into("<I", hdr, 8, kernel_size)
        p.write_bytes(bytes(hdr))
        return str(p)

    def _flasher(self, adb, target, image, log):
        self.flashed.append(target)
        return True

    def _seal(self, stock, target, force=False):
        msgs = []
        ok = PV.seal(_seal_adb(target=target), mock.Mock(), stock, log=msgs.append, wait=False,
                     flasher=self._flasher, force=force)
        return ok, "\n".join(msgs)

    def test_refuses_ramdisk_only_image_on_a_boot_partition(self):
        ok, log = self._seal(self._write(kernel_size=0), target="boot_a")
        self.assertFalse(ok)
        self.assertEqual(self.flashed, [])
        self.assertIn("kernel", log.lower())

    def test_refuses_full_boot_image_on_an_init_boot_partition(self):
        ok, _ = self._seal(self._write(kernel_size=39606288), target="init_boot_a")
        self.assertFalse(ok)
        self.assertEqual(self.flashed, [])

    def test_allows_matching_image_and_partition(self):
        ok, _ = self._seal(self._write(kernel_size=39606288), target="boot_a")
        self.assertTrue(ok)
        self.assertEqual(self.flashed, ["boot_a"])


if __name__ == "__main__":
    unittest.main()
