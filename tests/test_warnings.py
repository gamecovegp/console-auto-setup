# tests/test_warnings.py
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import warnings as W
from cas.adb import _parse_bootloader_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def dev(serial="MQ66x", state="device", model="AIR X", identity=None, fw=None,
        bootloader="unlocked", profile_name="airx", profile_has_golden=True,
        profile_model_match_ok=None):
    """A 'clean' device snapshot (no warnings) overridable per field."""
    if identity is None:
        identity = {"serial": serial, "device": "AIR_X", "soc": "SM6115",
                    "flash_target": "init_boot_b"}
    if fw is None:
        fw = {"firmware_id": "airx-mq66", "ok": True, "warnings": []}
    return {
        "serial": serial, "state": state, "model": model, "identity": identity,
        "fw": fw, "bootloader": bootloader, "profile_name": profile_name,
        "profile_has_golden": profile_has_golden,
        "profile_model_match_ok": profile_model_match_ok,
    }


def codes(warns):
    return {w["code"] for w in warns}


def by_code(warns, code):
    for w in warns:
        if w["code"] == code:
            return w
    return None


CLEAN_GLOBAL = {"library_reachable": True, "firmware_library_empty": False}


def ev(devices=None, **gstate):
    g = dict(CLEAN_GLOBAL)
    g.update(gstate)
    return W.evaluate(devices or [], g)


# ---------------------------------------------------------------------------
# Clean baseline
# ---------------------------------------------------------------------------

class TestClean(unittest.TestCase):
    def test_clean_device_no_warnings(self):
        self.assertEqual(ev([dev()]), [])

    def test_count_actionable_zero_when_clean(self):
        self.assertEqual(W.count_actionable(ev([dev()])), 0)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

class TestConnection(unittest.TestCase):
    def test_unauthorized_blocks_all_and_suppresses_other_checks(self):
        # even with empty identity, the only warning is the connection one
        warns = ev([dev(state="unauthorized", identity={}, fw={})])
        self.assertEqual(codes(warns), {"unauthorized"})
        w = by_code(warns, "unauthorized")
        self.assertEqual(w["severity"], "block")
        self.assertEqual(set(w["gates"]), {"root", "save", "download", "lock"})
        self.assertTrue(all(v == "block" for v in w["gates"].values()))

    def test_offline_states_map_to_offline(self):
        for st in ("offline", "recovery", "sideload", "no permissions", "weird"):
            warns = ev([dev(state=st, identity={}, fw={})])
            self.assertEqual(codes(warns), {"offline"}, st)
            self.assertEqual(by_code(warns, "offline")["severity"], "block")


# ---------------------------------------------------------------------------
# Root / brick-guard
# ---------------------------------------------------------------------------

class TestRootBlockers(unittest.TestCase):
    def test_no_flash_target_blocks_root_and_lock(self):
        idn = {"serial": "MQ66x", "flash_target": ""}
        w = by_code(ev([dev(identity=idn)]), "no_flash_target")
        self.assertIsNotNone(w)
        self.assertEqual(w["severity"], "block")
        self.assertEqual(w["gates"].get("root"), "block")
        self.assertEqual(w["gates"].get("lock"), "block")
        self.assertNotIn("download", w["gates"])

    def test_bootloader_locked_blocks_root_and_lock(self):
        w = by_code(ev([dev(bootloader="locked")]), "bootloader_locked")
        self.assertIsNotNone(w)
        self.assertEqual(w["severity"], "block")
        self.assertEqual(w["gates"].get("root"), "block")

    def test_bootloader_unknown_is_info_not_a_gate(self):
        w = by_code(ev([dev(bootloader="unknown")]), "bootloader_unknown")
        self.assertIsNotNone(w)
        self.assertEqual(w["severity"], "info")
        self.assertEqual(w["gates"], {})

    def test_fw_flash_mismatch_confirms_root_and_lock(self):
        fw = {"firmware_id": "airx", "ok": False,
              "warnings": ["firmware expects 'init_boot' but device exposes 'boot'"]}
        warns = ev([dev(fw=fw)])
        w = by_code(warns, "fw_flash_mismatch")
        self.assertIsNotNone(w)
        self.assertEqual(w["severity"], "confirm")
        self.assertEqual(w["gates"].get("root"), "confirm")
        self.assertNotIn("fw_variant_mismatch", codes(warns))

    def test_fw_variant_mismatch_from_device_and_serial_text(self):
        for txt in ("firmware device 'AIR_X' != device 'VIEGO'",
                    "serial 'MQ65x' matches none of ['MQ66']"):
            fw = {"firmware_id": "airx", "ok": False, "warnings": [txt]}
            warns = ev([dev(fw=fw)])
            self.assertIn("fw_variant_mismatch", codes(warns), txt)
            self.assertEqual(by_code(warns, "fw_variant_mismatch")["severity"], "confirm")

    def test_no_firmware_match_is_info(self):
        fw = {"firmware_id": None, "ok": False, "warnings": ["no match — select manually"]}
        w = by_code(ev([dev(fw=fw)]), "no_firmware_match")
        self.assertIsNotNone(w)
        self.assertEqual(w["severity"], "info")

    def test_profile_model_mismatch_confirms_root_lock(self):
        w = by_code(ev([dev(profile_model_match_ok=False)]), "profile_model_mismatch")
        self.assertIsNotNone(w)
        self.assertEqual(w["gates"].get("root"), "confirm")
        self.assertEqual(w["gates"].get("lock"), "confirm")


# ---------------------------------------------------------------------------
# Profile / library
# ---------------------------------------------------------------------------

class TestProfileLibrary(unittest.TestCase):
    def test_no_profile_blocks_download_confirms_root(self):
        for name in (None, "(no match)"):
            w = by_code(ev([dev(profile_name=name)]), "no_profile")
            self.assertIsNotNone(w, name)
            self.assertEqual(w["gates"].get("download"), "block")
            self.assertEqual(w["gates"].get("root"), "confirm")
            self.assertEqual(w["severity"], "block")

    def test_no_golden_blocks_download_only(self):
        w = by_code(ev([dev(profile_has_golden=False)]), "no_golden")
        self.assertIsNotNone(w)
        self.assertEqual(w["gates"], {"download": "block"})

    def test_identity_incomplete_is_info(self):
        idn = {"serial": "", "flash_target": "init_boot_b"}
        w = by_code(ev([dev(identity=idn)]), "identity_incomplete")
        self.assertIsNotNone(w)
        self.assertEqual(w["severity"], "info")

    def test_library_unreachable_is_global_block(self):
        w = by_code(ev([dev()], library_reachable=False), "library_unreachable")
        self.assertIsNotNone(w)
        self.assertEqual(w["scope"], "global")
        self.assertIsNone(w["serial"])
        self.assertEqual(w["gates"].get("download"), "block")
        self.assertEqual(w["gates"].get("save"), "block")

    def test_firmware_library_empty_is_global_info(self):
        w = by_code(ev([dev()], firmware_library_empty=True), "firmware_library_empty")
        self.assertIsNotNone(w)
        self.assertEqual(w["scope"], "global")
        self.assertEqual(w["severity"], "info")


# ---------------------------------------------------------------------------
# count_actionable + gate()
# ---------------------------------------------------------------------------

class TestGate(unittest.TestCase):
    def test_count_actionable_excludes_info(self):
        # one block (no_flash_target) + one info (bootloader_unknown) => 1 actionable
        warns = ev([dev(identity={"serial": "s", "flash_target": ""}, bootloader="unknown")])
        self.assertEqual(W.count_actionable(warns), 1)

    def test_gate_partitions_block_vs_confirm_for_serial_and_action(self):
        warns = ev([
            dev(serial="A", identity={"serial": "A", "flash_target": ""}),   # block root
            dev(serial="B", profile_model_match_ok=False),                   # confirm root
        ])
        ga = W.gate(warns, "A", ["root"])
        self.assertEqual([w["code"] for w in ga["block"]], ["no_flash_target"])
        self.assertEqual(ga["confirm"], [])
        gb = W.gate(warns, "B", ["root"])
        self.assertEqual(gb["block"], [])
        self.assertEqual([w["code"] for w in gb["confirm"]], ["profile_model_mismatch"])

    def test_gate_global_with_serial_none(self):
        warns = ev([dev()], library_reachable=False)
        g = W.gate(warns, None, ["download"])
        self.assertEqual([w["code"] for w in g["block"]], ["library_unreachable"])
        # a device serial should NOT pick up the global block
        self.assertEqual(W.gate(warns, "MQ66x", ["download"])["block"], [])


# ---------------------------------------------------------------------------
# adb._parse_bootloader_state
# ---------------------------------------------------------------------------

class TestBootloaderParse(unittest.TestCase):
    def test_vbmeta_device_state(self):
        self.assertEqual(_parse_bootloader_state({"ro.boot.vbmeta.device_state": "locked"}), "locked")
        self.assertEqual(_parse_bootloader_state({"ro.boot.vbmeta.device_state": "unlocked"}), "unlocked")

    def test_verifiedbootstate(self):
        self.assertEqual(_parse_bootloader_state({"ro.boot.verifiedbootstate": "orange"}), "unlocked")
        self.assertEqual(_parse_bootloader_state({"ro.boot.verifiedbootstate": "green"}), "locked")
        self.assertEqual(_parse_bootloader_state({"ro.boot.verifiedbootstate": "yellow"}), "locked")

    def test_vbmeta_wins_over_verifiedboot(self):
        props = {"ro.boot.vbmeta.device_state": "unlocked", "ro.boot.verifiedbootstate": "green"}
        self.assertEqual(_parse_bootloader_state(props), "unlocked")

    def test_unknown_when_absent(self):
        self.assertEqual(_parse_bootloader_state({}), "unknown")
        self.assertEqual(_parse_bootloader_state({"ro.boot.verifiedbootstate": ""}), "unknown")


if __name__ == "__main__":
    unittest.main()
