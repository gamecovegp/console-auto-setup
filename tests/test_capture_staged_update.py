# tests/test_capture_staged_update.py
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import provision as PV

FP = "MANGMI/MANGMI/AIR_X:14/AIR_X_user_20260507/eng.hxh.20260507.141302:user/release-keys"


def _adb_with_su(responses, slot="_a"):
    """responses: dict mapping a substring of the command -> (rc, out, err)."""
    adb = mock.Mock()
    adb.slot_suffix.return_value = slot
    adb.boot_partition.return_value = "init_boot"
    adb.getprop.return_value = FP
    adb.serial = "TESTSERIAL"

    def _su(cmd, timeout=900):
        for key, resp in responses.items():
            if key in cmd:
                return resp
        return (1, "", "unknown command")

    adb.su.side_effect = _su
    return adb


class TestUpdateStagedProbe(unittest.TestCase):
    def test_idle_status_is_not_staged(self):
        adb = _adb_with_su({"update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_IDLE", "")})
        self.assertIs(PV._update_is_staged(adb), False)

    def test_need_reboot_status_is_staged(self):
        adb = _adb_with_su({"update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_UPDATED_NEED_REBOOT", "")})
        self.assertIs(PV._update_is_staged(adb), True)

    def test_downloading_status_is_staged(self):
        adb = _adb_with_su({"update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_DOWNLOADING", "")})
        self.assertIs(PV._update_is_staged(adb), True)

    def test_falls_back_to_bootctl_when_update_engine_unavailable(self):
        adb = _adb_with_su({
            "update_engine_client": (127, "", "not found"),
            "bootctl": (0, "0", ""),          # inactive slot NOT marked successful -> staged
        })
        self.assertIs(PV._update_is_staged(adb), True)

    def test_bootctl_successful_slot_is_not_staged(self):
        adb = _adb_with_su({
            "update_engine_client": (127, "", "not found"),
            "bootctl": (0, "1", ""),
        })
        self.assertIs(PV._update_is_staged(adb), False)

    def test_neither_probe_readable_is_undeterminable(self):
        adb = _adb_with_su({})
        self.assertIsNone(PV._update_is_staged(adb))

    def test_su_commands_contain_no_shell_operators(self):
        """adb space-joins argv, so `su -c` must receive ONE command with no &&/||/;."""
        seen = []
        adb = _adb_with_su({"update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_IDLE", "")})
        orig = adb.su.side_effect

        def _record(cmd, timeout=900):
            seen.append(cmd)
            return orig(cmd, timeout)

        adb.su.side_effect = _record
        PV._update_is_staged(adb)
        for cmd in seen:
            for op in ("&&", "||", ";", '"', "'"):
                self.assertNotIn(op, cmd, f"{op!r} in su command {cmd!r}")


class TestCaptureRefusesStagedUpdate(unittest.TestCase):
    def test_capture_skipped_when_update_staged(self):
        adb = _adb_with_su({
            "update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_UPDATED_NEED_REBOOT", ""),
            "dd": (0, "", ""),
        })
        with tempfile.TemporaryDirectory() as td:
            msgs = []
            self.assertFalse(PV.capture_factory_init_boot(adb, td, log=msgs.append))
            self.assertTrue(any("staged" in m or "update" in m for m in msgs), msgs)

    def test_capture_skipped_when_undeterminable(self):
        adb = _adb_with_su({"dd": (0, "", "")})
        with tempfile.TemporaryDirectory() as td:
            msgs = []
            self.assertFalse(PV.capture_factory_init_boot(adb, td, log=msgs.append))

    def test_no_dd_is_issued_when_refused(self):
        adb = _adb_with_su({
            "update_engine_client": (0, "CURRENT_OP=UPDATE_STATUS_UPDATED_NEED_REBOOT", ""),
        })
        with tempfile.TemporaryDirectory() as td:
            PV.capture_factory_init_boot(adb, td, log=lambda m: None)
        issued = [c.args[0] for c in adb.su.call_args_list]
        self.assertFalse(any(c.startswith("dd ") for c in issued), issued)


if __name__ == "__main__":
    unittest.main()
