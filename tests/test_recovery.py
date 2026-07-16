"""Tests for the recovery-guidance catalog (cas/recovery.py). Pure — no device, no Tk."""
import sys, pathlib, unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from cas import recovery as R
from cas.recovery import DeviceMode as M

OPS = ["root", "save", "download", "warmup", "lock"]


class TestAdvise(unittest.TestCase):
    def test_every_operation_x_mode_gives_nonempty_ordered_steps(self):
        for op in OPS:
            for mode in M:
                rec = R.advise(op, "", mode)
                self.assertEqual(rec.operation, op)
                self.assertTrue(rec.steps, f"{op}/{mode} has no steps")
                self.assertTrue(rec.state_label, f"{op}/{mode} has no state label")

    def test_edl_says_hold_power_and_names_the_operation(self):
        rec = R.advise("root", "edl_flash", M.EDL_9008)
        blob = " ".join(rec.steps).lower()
        self.assertIn("hold", blob)
        self.assertIn("power", blob)
        self.assertIn("12", blob)                       # ~12s hold
        self.assertIn("root", " ".join(rec.steps).lower())   # retry verb names the op

    def test_fastboot_says_fastboot_reboot(self):
        rec = R.advise("root", "fastboot_flash", M.FASTBOOT)
        self.assertIn("fastboot reboot", " ".join(rec.steps).lower())

    def test_fastbootd_uses_the_same_reboot_advice_as_fastboot(self):
        self.assertIn("fastboot reboot", " ".join(R.advise("root", "", M.FASTBOOTD).steps).lower())

    def test_offline_says_replug_data_cable(self):
        rec = R.advise("download", "push", M.ADB_OFFLINE)
        blob = " ".join(rec.steps).lower()
        self.assertIn("replug", blob)
        self.assertIn("data", blob)                     # data cable, not charge-only

    def test_absent_with_edl_phase_gives_edl_advice(self):
        # tiebreaker: vanished during an EDL write -> almost certainly dark in 9008
        rec = R.advise("root", "edl_flash", M.ABSENT)
        self.assertIn("12", " ".join(rec.steps))        # EDL hold-power advice, not the generic absent one
        self.assertEqual(rec.state_label, R.advise("root", "", M.EDL_9008).state_label)

    def test_absent_with_fastboot_phase_gives_fastboot_advice(self):
        rec = R.advise("root", "fastboot_flash", M.ABSENT)
        self.assertIn("fastboot reboot", " ".join(rec.steps).lower())

    def test_sealed_ok_is_not_attention(self):
        rec = R.advise("lock", "done", M.SEALED_OK)
        self.assertFalse(rec.needs_attention)

    def test_windows_branch_points_at_setup_windows_bat(self):
        with mock.patch.object(R, "_is_windows", lambda: True):
            blob = " ".join(R.advise("root", "fastboot_flash", M.FASTBOOT).steps)
        self.assertIn("setup-windows.bat", blob)

    def test_posix_branch_points_at_udev_not_bat(self):
        with mock.patch.object(R, "_is_windows", lambda: False):
            blob = " ".join(R.advise("root", "fastboot_flash", M.FASTBOOT).steps).lower()
        self.assertIn("udev", blob)
        self.assertNotIn("setup-windows.bat", blob)

    def test_operation_safety_note_present(self):
        self.assertIn("untouched", " ".join(R.advise("save", "capture", M.ADB_OFFLINE).steps).lower())
        self.assertIn("idempotent", " ".join(R.advise("download", "push", M.ADB_OFFLINE).steps).lower())


class TestRenderers(unittest.TestCase):
    def test_row_hint_is_one_line(self):
        rec = R.advise("root", "edl_flash", M.EDL_9008)
        self.assertNotIn("\n", rec.row_hint())
        self.assertIn(rec.state_label.split(" ")[0][:3].lower(), rec.row_hint().lower())

    def test_popup_line_carries_the_serial(self):
        rec = R.advise("root", "edl_flash", M.EDL_9008)
        self.assertIn("MQ66", rec.popup_line("MQ66123"))

    def test_log_block_is_multiline_with_state_and_steps(self):
        block = R.advise("root", "edl_flash", M.EDL_9008).log_block()
        self.assertIn("\n", block)
        self.assertIn("EDL", block)


class TestSummaryPopup(unittest.TestCase):
    def test_none_when_nothing_needs_attention(self):
        recs = {"A": R.advise("lock", "done", M.SEALED_OK), "B": None}
        self.assertIsNone(R.summary_popup(recs, "Lock"))

    def test_lists_each_attention_device_once(self):
        recs = {
            "MQ66A": R.advise("root", "edl_flash", M.EDL_9008),
            "RP6B": R.advise("root", "fastboot_flash", M.FASTBOOT),
            "OK1": R.advise("lock", "done", M.SEALED_OK),   # excluded
        }
        text = R.summary_popup(recs, "Root")
        self.assertIn("MQ66A", text)
        self.assertIn("RP6B", text)
        self.assertNotIn("OK1", text)
        self.assertIn("2", text)                            # "2 device(s) need attention"


class _FakeAdb:
    def __init__(self, state):
        self._state = state
    def state(self):
        return self._state


class _FakeFb:
    def __init__(self, present):
        self._present = present
    def devices(self):
        return "SERIAL123\tfastboot\n" if self._present else ""


class TestProbeMode(unittest.TestCase):
    def _probe(self, state, fb_present=False, edl=()):
        return R.probe_mode(_FakeAdb(state), _FakeFb(fb_present), edl_ports=lambda: list(edl))

    def test_device_state_is_booted(self):
        self.assertIs(self._probe("device"), M.BOOTED_ADB)

    def test_offline_is_adb_offline(self):
        self.assertIs(self._probe("offline"), M.ADB_OFFLINE)

    def test_unauthorized_is_adb_offline(self):
        self.assertIs(self._probe("unauthorized"), M.ADB_OFFLINE)

    def test_absent_in_adb_but_in_fastboot_is_fastboot(self):
        self.assertIs(self._probe("", fb_present=True), M.FASTBOOT)

    def test_absent_in_adb_and_fastboot_but_edl_port_present_is_edl(self):
        self.assertIs(self._probe("", fb_present=False, edl=["/dev/ttyUSB0"]), M.EDL_9008)

    def test_nothing_anywhere_is_absent(self):
        self.assertIs(self._probe("", fb_present=False, edl=[]), M.ABSENT)

    def test_a_probe_exception_never_raises(self):
        class Boom:
            def state(self):
                raise RuntimeError("adb died")
        self.assertIs(R.probe_mode(Boom(), _FakeFb(False), edl_ports=lambda: []), M.ABSENT)


if __name__ == "__main__":
    unittest.main()
