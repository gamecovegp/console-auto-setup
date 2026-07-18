# tests/test_seal_capture.py
import os
import sys
import pathlib
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import provision as PV
from cas import initboot_store as IBS

FP = "qti/kalama/kalama:13/TKQ1.231222.001/eng.RP6.20260119.170007:user/release-keys"


def _fake_adb(slot="_a", su_rc=0, pull_bytes=b"ANDROID!" + b"\x00" * 1024, fp=FP):
    adb = mock.Mock()
    adb.slot_suffix.return_value = slot
    adb.getprop.return_value = fp
    adb.su.return_value = (su_rc, "", "")

    def _pull(src, dst):
        if pull_bytes is None:
            return False
        pathlib.Path(dst).write_bytes(pull_bytes)
        return True

    adb.pull.side_effect = _pull
    return adb


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = pathlib.Path(self.tmp) / "_init_boot_factory"

    def test_captures_inactive_slot_b_when_active_a(self):
        adb = _fake_adb(slot="_a")
        ok = PV.capture_factory_init_boot(adb, self.store, log=lambda *a: None)
        self.assertTrue(ok)
        self.assertTrue(IBS.has(self.store, FP))
        # dumped the INACTIVE slot (_b) since active is _a
        ddcmd = adb.su.call_args[0][0]
        self.assertIn("init_boot_b", ddcmd)

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
        adb = _fake_adb()
        with mock.patch.object(PV._ibs, "put", side_effect=OSError("disk full")):
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


if __name__ == "__main__":
    unittest.main()
