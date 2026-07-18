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


if __name__ == "__main__":
    unittest.main()
