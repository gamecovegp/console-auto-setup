# tests/test_firmware.py
import os
import sys
import json
import pathlib
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas.adb import Adb
from cas import config as C
from cas import firmware as FW


# --- CAS_CONFIG isolation (module-wide safety net) ------------------------------------------------
# INVARIANT: no test in this module may EVER write the operator's real cas-config.json (gitignored, at
# the repo root — see cas/config.py's config_path()). Every test that currently writes it (via
# firmware.set_device_firmware / resolve()'s auto-match) already isolates CAS_CONFIG in its own
# setUp()/tearDown(); this module-level default is a backstop for whatever a test — present or future —
# forgets to isolate itself. Mirrors tests/test_cas.py's setUpModule/tearDownModule.
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


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

class IdRunner:
    """getprop runner returning a fixed prop table; everything else returns empty ok."""
    def __init__(self, props):
        self.props = props

    def __call__(self, args, input_text=None, timeout=900):
        if "shell" in args and args[-1].startswith("getprop"):
            return 0, (self.props.get(args[-1].split()[-1], "") + "\n"), ""
        return 0, "", ""


AIRX_PROPS = {
    "ro.serialno": "MQ66142509130541", "ro.product.device": "AIR_X",
    "ro.product.model": "AIR X", "ro.product.manufacturer": "MANGMI",
    "ro.soc.model": "SM6115", "ro.mangmi.dev.code": "MQ66",
    "ro.product.first_api_level": "33", "ro.boot.slot_suffix": "_b",
    "ro.board.platform": "bengal", "ro.build.version.release": "14",
    "ro.boot.bootdevice": "4804000.sdhci",
}


def make_fw(root, fid, device="AIR_X", flash="init_boot", storage="emmc",
            match=None, current="20260507-165105"):
    d = pathlib.Path(root) / fid
    (d / "versions" / current / "payload").mkdir(parents=True)
    FW._write_json(d / "meta.json", {
        "id": fid, "label": fid, "device": device, "flash_target": flash,
        "storage": storage, "match": match or {}, "current": current, "history": []})
    return FW.Firmware(d)


def fake_build(tmp, name, storage="emmc", with_init_boot=True, device="AIR_X",
               dev_code="MQ66", os_version="1.1.6", board_platform="bengal",
               soc="SM6115", android="14"):
    """A minimal device-firmware tree: <name>/<storage>/{rawprogram1.xml, super_1.img}.
    board_platform/soc/android are written into super_1.img so detect_build() can grep them; pass '' to
    simulate a build whose props are undetectable (the legacy-entry case)."""
    d = pathlib.Path(tmp) / name
    p = d / storage
    p.mkdir(parents=True)
    parts = '<program label="boot_a" /><program label="init_boot_a" />' if with_init_boot \
        else '<program label="boot_a" /><program label="boot_b" />'
    (p / "rawprogram1.xml").write_text(f"<data>{parts}</data>")
    props = (f"ro.product.system.device={device}\nro.mangmi.dev.code={dev_code}\n"
             f"ro.mangmi.os.version={os_version}\n")
    if board_platform:
        props += f"ro.board.platform={board_platform}\n"
    if soc:
        props += f"ro.soc.model={soc}\n"
    if android:
        props += f"ro.build.version.release={android}\n"
    (p / "super_1.img").write_text(props)
    return d


# ---------------------------------------------------------------------------
# Task 1 (adapted): identity() free function in firmware.py
# ---------------------------------------------------------------------------

class TestIdentity(unittest.TestCase):
    def test_identity_airx(self):
        idn = FW.identity(Adb(runner=IdRunner(AIRX_PROPS)))
        self.assertEqual(idn["serial"], "MQ66142509130541")
        self.assertEqual(idn["device"], "AIR_X")
        self.assertEqual(idn["soc"], "SM6115")
        self.assertEqual(idn["flash_target"], "init_boot_b")

    def test_identity_carries_gate_props(self):
        idn = FW.identity(Adb(runner=IdRunner(AIRX_PROPS)))
        self.assertEqual(idn["board_platform"], "bengal")
        self.assertEqual(idn["android_release"], "14")
        self.assertEqual(idn["bootdevice"], "4804000.sdhci")
        self.assertEqual(idn["soc"], "SM6115")      # unchanged

    def test_identity_gate_props_absent_are_empty_not_missing(self):
        # A device that doesn't report them must yield '' (abstain), never a KeyError.
        idn = FW.identity(Adb(runner=IdRunner({"ro.serialno": "X"})))
        self.assertEqual(idn["board_platform"], "")
        self.assertEqual(idn["android_release"], "")
        self.assertEqual(idn["bootdevice"], "")


# ---------------------------------------------------------------------------
# Task 2 (adapted): firmware_root / get_device_firmware / set_device_firmware
#                   all live in firmware.py (not config.py)
# ---------------------------------------------------------------------------

class TestDeviceFirmware(unittest.TestCase):
    def setUp(self):
        # CAS_CONFIG isolation: save whatever it was (the module default from setUpModule) so tearDown can
        # RESTORE it instead of assuming it was unset — a bare pop() here re-opens the bug commit 1492ce8
        # closed (the suite falling through to the operator's real, gitignored cas-config.json).
        self._saved_cas_config = os.environ.get("CAS_CONFIG")
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        os.environ["CAS_PROFILES"] = self.tmp   # pins library_root() to tmp

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config
        os.environ.pop("CAS_PROFILES", None)

    def test_firmware_root_under_library(self):
        self.assertEqual(FW.firmware_root(), pathlib.Path(self.tmp) / "_firmware")

    def test_set_get_roundtrip_and_forget(self):
        FW.set_device_firmware("MQ66x", "mangmi-air-x-mq66", manual=True)
        got = FW.get_device_firmware()["MQ66x"]
        self.assertEqual(got["firmware_id"], "mangmi-air-x-mq66")
        self.assertTrue(got["manual"])
        self.assertIsNone(got["version"])
        FW.set_device_firmware("MQ66x", None)   # forget
        self.assertNotIn("MQ66x", FW.get_device_firmware())

    def test_pinned_version_persists(self):
        FW.set_device_firmware("S", "fw", version="20260507-165105", manual=True)
        self.assertEqual(FW.get_device_firmware()["S"]["version"], "20260507-165105")


# ---------------------------------------------------------------------------
# Task 3: Firmware class + list_firmware + find
# ---------------------------------------------------------------------------

class TestFirmwareClass(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        self.root.mkdir(parents=True)

    def test_list_and_find_and_props(self):
        make_fw(self.root, "mangmi-air-x-mq66", match={"serial_prefix": ["MQ66"]})
        fws = FW.list_firmware(self.root)
        self.assertEqual([f.id for f in fws], ["mangmi-air-x-mq66"])
        f = FW.find("mangmi-air-x-mq66", self.root)
        self.assertEqual(f.flash_target, "init_boot")
        self.assertEqual(f.current(), "20260507-165105")
        self.assertEqual(f.versions(), ["20260507-165105"])
        self.assertTrue(f.payload_dir().is_dir())

    def test_find_missing(self):
        self.assertIsNone(FW.find("nope", self.root))

    def test_nondirectory_entry_not_listed_as_firmware(self):
        # index.json is skipped because it is a file (not a dir), not because of its name
        (self.root / "index.json").write_text("{}")
        make_fw(self.root, "ayn-m0", device="AYN", flash="boot", storage="ufs")
        self.assertEqual([f.id for f in FW.list_firmware(self.root)], ["ayn-m0"])


# ---------------------------------------------------------------------------
# Task 2b: _storage_from_bootdevice() + _android_major() — gate helpers
# ---------------------------------------------------------------------------

class TestStorageProbe(unittest.TestCase):
    """ro.boot.bootdevice -> 'ufs'|'emmc'|''. UNVERIFIED against real hardware: the '' fallback is what
    makes a wrong guess safe (unrecognized -> axis abstains -> legacy behavior, never a wrong flash)."""

    def test_ufs_controller(self):
        self.assertEqual(FW._storage_from_bootdevice("1d84000.ufshc"), "ufs")

    def test_emmc_sdhci_controller(self):
        self.assertEqual(FW._storage_from_bootdevice("4804000.sdhci"), "emmc")

    def test_emmc_mmc_controller(self):
        self.assertEqual(FW._storage_from_bootdevice("7c4000.mmc0"), "emmc")

    def test_case_insensitive(self):
        self.assertEqual(FW._storage_from_bootdevice("1D84000.UFSHC"), "ufs")

    def test_unknown_returns_empty_so_axis_abstains(self):
        self.assertEqual(FW._storage_from_bootdevice("something.weird"), "")

    def test_none_and_empty_return_empty(self):
        self.assertEqual(FW._storage_from_bootdevice(None), "")
        self.assertEqual(FW._storage_from_bootdevice(""), "")


class TestAndroidMajor(unittest.TestCase):
    """ro.build.version.release -> major version string; '' -> ''."""

    def test_android_13(self):
        self.assertEqual(FW._android_major("13"), "13")

    def test_android_13_1(self):
        self.assertEqual(FW._android_major("13.1"), "13")

    def test_android_14(self):
        self.assertEqual(FW._android_major("14"), "14")

    def test_android_14_with_patch(self):
        self.assertEqual(FW._android_major("14.0.1"), "14")

    def test_empty_string(self):
        self.assertEqual(FW._android_major(""), "")

    def test_none_returns_empty(self):
        self.assertEqual(FW._android_major(None), "")

    def test_whitespace_stripped(self):
        self.assertEqual(FW._android_major("  13  "), "13")


# ---------------------------------------------------------------------------
# Task 4: match() — suggestion by identity
# ---------------------------------------------------------------------------

class TestMatch(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        self.root.mkdir(parents=True)
        make_fw(self.root, "mangmi-air-x-mq66",
                match={"serial_prefix": ["MQ66"], "device": "AIR_X", "soc": "SM6115"})
        make_fw(self.root, "mangmi-air-x-mq65",
                match={"serial_prefix": ["MQ65"], "device": "AIR_X", "soc": "SM6115"})
        make_fw(self.root, "mangmi-pocket-max", device="Pocket_Max", flash="boot", storage="ufs",
                match={"device": "Pocket_Max"})

    def test_serial_prefix_splits_airx(self):
        m = FW.match({"serial": "MQ66999", "device": "AIR_X", "soc": "SM6115", "brand": "MANGMI"},
                     self.root)
        self.assertEqual(m[0].id, "mangmi-air-x-mq66")
        m = FW.match({"serial": "MQ65111", "device": "AIR_X", "soc": "SM6115", "brand": "MANGMI"},
                     self.root)
        self.assertEqual(m[0].id, "mangmi-air-x-mq65")

    def test_pocket_max_by_device(self):
        m = FW.match({"serial": "PKX1", "device": "Pocket_Max", "brand": "MANGMI"}, self.root)
        self.assertEqual(m[0].id, "mangmi-pocket-max")

    def test_returns_current_version(self):
        m = FW.match({"serial": "MQ66999", "device": "AIR_X", "soc": "SM6115"}, self.root)
        self.assertEqual(m[1], "20260507-165105")

    def test_no_match_returns_none(self):
        self.assertIsNone(FW.match({"serial": "ZZ", "device": "OTHER"}, self.root))

    def test_gate_rejected_firmware_cannot_be_promoted_by_serial_prefix(self):
        # The wrong-chip build must be the TOP soft scorer, or the gate is never exercised.
        root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        root.mkdir(parents=True)
        make_fw(root, "wrong-chip-but-serial-hit",
                match={"serial_prefix": ["MQ66"], "device": "AIR_X", "board_platform": "sun"})
        make_fw(root, "right-chip-weak-rules", match={"device": "AIR_X", "board_platform": "bengal"})
        m = FW.match({"serial": "MQ66999", "device": "AIR_X", "board_platform": "bengal",
                      "soc": "SM6115", "brand": "MANGMI"}, root)
        self.assertEqual(m[0].id, "right-chip-weak-rules")   # old code: wrong-chip, 5 vs 2

    def test_gate_rejected_firmware_is_not_a_candidate_even_when_alone(self):
        root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        root.mkdir(parents=True)
        make_fw(root, "wrong-chip-but-serial-hit",
                match={"serial_prefix": ["MQ66"], "device": "AIR_X", "board_platform": "sun"})
        self.assertIsNone(FW.match({"serial": "MQ66999", "device": "AIR_X",
                                    "board_platform": "bengal"}, root))

    def test_soc_is_not_scored(self):
        # soc is a gate, not a tiebreaker: both score 2 on device alone -> tie -> None.
        root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        root.mkdir(parents=True)
        make_fw(root, "aaa-soc-rule", match={"device": "AIR_X", "soc": "SM6115"})
        make_fw(root, "bbb-no-soc-rule", match={"device": "AIR_X"})
        self.assertIsNone(FW.match({"serial": "MQ66999", "device": "AIR_X", "soc": "SM6115"}, root))

    def test_affirmed_gate_pass_is_a_candidate_at_score_zero(self):
        # THE MOTIVATING CASE: an RP6 on the Odin 2 build hits no serial prefix, and its device and
        # brand both differ -> score 0. The affirmed gate pass alone must carry it.
        root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        root.mkdir(parents=True)
        make_fw(root, "ayn-odin2", device="odin2", storage="ufs",
                match={"device": "odin2", "board_platform": "kalama", "android_release": "13"})
        m = FW.match({"serial": "RP6x", "device": "RP6", "brand": "Retroid",
                      "board_platform": "kalama", "soc": "SM8550", "android_release": "13",
                      "bootdevice": "1d84000.ufshc"}, root)
        self.assertIsNotNone(m)
        self.assertEqual(m[0].id, "ayn-odin2")

    def test_vacuous_gate_pass_at_score_zero_is_not_a_candidate(self):
        # A legacy chip-less entry affirms nothing. It must still need a positive score - today's
        # behavior, preserved. Otherwise every legacy entry would tie at 0 and matching would break.
        root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        root.mkdir(parents=True)
        make_fw(root, "legacy", device="whatever", storage="", match={})
        self.assertIsNone(FW.match({"serial": "RP6x", "device": "RP6",
                                    "board_platform": "kalama"}, root))

    def test_storage_only_agreement_is_not_a_match_candidate(self):
        # C1: a firmware with only a storage rule and no chip recorded must NOT become a candidate at
        # score 0 just because storage agrees. This is the exact mechanism that let "odin3" (a
        # permanent UFS wildcard — its payload has no super image, so backfill can never give it a
        # chip) auto-select onto ANY ufs device, including wrong-chip ones, once a tie-breaking
        # legacy competitor picked up a chip via backfill.
        root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        root.mkdir(parents=True)
        make_fw(root, "odin3-like", storage="ufs", match={})
        self.assertIsNone(FW.match({"serial": "X", "device": "OTHER",
                                    "bootdevice": "1d84000.ufshc"}, root))


# ---------------------------------------------------------------------------
# Task 4b: gate_check() — the core rule
# ---------------------------------------------------------------------------

class TestGateCheck(unittest.TestCase):
    """CORE RULE: reject only on a KNOWN CONFLICT (same field populated both sides, values differ);
    never on missing data."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        self.root.mkdir(parents=True)

    def _fw(self, fid="ayn-odin2", storage="ufs", **rules):
        return make_fw(self.root, fid, device="odin2", storage=storage, match=rules)

    def _rp6(self, **over):
        idn = {"serial": "RP6x", "device": "RP6", "brand": "Retroid", "board_platform": "kalama",
               "soc": "SM8550", "android_release": "13", "bootdevice": "1d84000.ufshc"}
        idn.update(over)
        return idn

    # --- the motivating case ---------------------------------------------------------------------
    def test_proven_cross_brand_pair_passes_and_is_affirmed(self):
        # RP6 on the Odin 2 build: known to boot. Must PASS and must be AFFIRMED (agreed>0), or
        # match() would discard it at score 0. All four axes are populated on both sides and agree
        # (board_platform, soc, android_release, storage), but ONLY board_platform/soc count into
        # `agreed` (C1 fix) — android_release and storage corroborate but don't affirm the chip.
        fw = self._fw(board_platform="kalama", soc="SM8550", android_release="13")
        ok, reason, agreed = FW.gate_check(fw, self._rp6())
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(agreed, 2)

    # --- known conflicts reject ------------------------------------------------------------------
    def test_chip_conflict_rejects(self):
        fw = self._fw(board_platform="sun")                      # Odin 3 build
        ok, reason, agreed = FW.gate_check(fw, self._rp6())      # kalama unit
        self.assertFalse(ok)
        self.assertIn("kalama", reason)
        self.assertEqual(agreed, 0)

    def test_soc_conflict_rejects(self):
        # Firmware records no board_platform, so soc is the sole/fallback chip axis under test. (A
        # board_platform AGREEMENT now outranks a soc conflict instead of rejecting — see
        # test_platform_agreement_outranks_a_soc_conflict below — so this scenario no longer pairs an
        # agreeing platform with a conflicting soc; that combination is covered separately.)
        fw = self._fw(soc="SM8750")
        ok, reason, agreed = FW.gate_check(fw, self._rp6())
        self.assertFalse(ok)
        self.assertIn("SM8550", reason)
        self.assertEqual(agreed, 0)

    def test_android_major_conflict_rejects(self):
        # board_platform agrees before android conflicts — would otherwise leak agreed=1 out of a reject.
        fw = self._fw(board_platform="kalama", android_release="15")
        ok, reason, agreed = FW.gate_check(fw, self._rp6())
        self.assertFalse(ok)
        self.assertIn("android", reason)
        self.assertEqual(agreed, 0)

    def test_storage_conflict_rejects(self):
        # board_platform, soc, AND android_release all agree before storage conflicts — this is the
        # case that reaches agreed=3 on a firmware that would BRICK the unit if agreed weren't zeroed
        # on every reject path. PINS finding: agreed must be 0 whenever ok is False.
        fw = self._fw(storage="emmc", board_platform="kalama",  # ufs unit, emmc firmware
                       soc="SM8550", android_release="13")
        ok, reason, agreed = FW.gate_check(fw, self._rp6())
        self.assertFalse(ok)
        self.assertIn("storage", reason)
        self.assertEqual(agreed, 0)

    # --- missing data ABSTAINS (never rejects) ----------------------------------------------------
    def test_legacy_entry_with_no_chip_abstains_vacuously(self):
        fw = self._fw(storage="")                                # today's meta.json: no gate fields
        ok, reason, agreed = FW.gate_check(fw, self._rp6())
        self.assertTrue(ok)
        self.assertEqual(agreed, 0)                              # vacuous: affirmed nothing

    def test_device_not_reporting_props_abstains(self):
        fw = self._fw(board_platform="kalama", android_release="13")
        ok, _, agreed = FW.gate_check(fw, self._rp6(board_platform="", android_release="",
                                                    soc="", bootdevice=""))
        self.assertTrue(ok)
        self.assertEqual(agreed, 0)

    def test_unrecognized_bootdevice_makes_storage_abstain_not_reject(self):
        fw = self._fw(storage="emmc", board_platform="kalama")
        ok, _, _ = FW.gate_check(fw, self._rp6(bootdevice="something.weird"))
        self.assertTrue(ok)                                      # storage abstained, chip agreed

    # --- the cross-prop trap ----------------------------------------------------------------------
    def test_never_compares_board_platform_against_soc(self):
        # fw records only soc; device reports only board_platform. 'kalama' vs 'SM8550' must NOT
        # be read as a conflict — they name the same silicon.
        # bootdevice is neutralized too: _fw()'s default storage="ufs" would otherwise silently AGREE
        # against _rp6()'s default ufs bootdevice, contaminating the agreed count this test isolates.
        fw = self._fw(soc="SM8550")
        ok, _, agreed = FW.gate_check(fw, self._rp6(soc="", bootdevice=""))
        self.assertTrue(ok)
        self.assertEqual(agreed, 0)

    # --- comparison semantics ---------------------------------------------------------------------
    def test_android_minor_does_not_conflict(self):
        fw = self._fw(board_platform="kalama", android_release="13")
        ok, _, _ = FW.gate_check(fw, self._rp6(android_release="13.1"))
        self.assertTrue(ok)

    def test_chip_compare_is_case_insensitive(self):
        # board_platform agrees case-insensitively; storage also agrees (fw default storage="ufs" vs
        # rp6's ufs bootdevice) but storage does NOT count into `agreed` (C1 fix) -> exact count of 1
        # so a dropped/over-counted axis is observable.
        fw = self._fw(board_platform="KALAMA")
        ok, _, agreed = FW.gate_check(fw, self._rp6())
        self.assertTrue(ok)
        self.assertEqual(agreed, 1)

    # --- per-axis contribution isolation -----------------------------------------------------------
    def test_chip_only_axis_populated_agreed_is_exactly_one(self):
        # Backfilled-chip entry: only board_platform is recorded on the firmware (no soc, no
        # android_release, no storage) — chip agreement alone must be the SOLE contributor to agreed.
        # This is the production case that keeps a proven cross-brand pair (RP6 on the AYN Odin 2
        # build) a candidate when storage wasn't captured on ingest.
        fw = self._fw(storage="", board_platform="kalama")
        ok, _, agreed = FW.gate_check(fw, self._rp6(bootdevice=""))
        self.assertTrue(ok)
        self.assertEqual(agreed, 1)

    # --- C1: storage-only agreement must not confer candidacy --------------------------------------
    def test_storage_only_agreement_is_vacuous_not_affirmed(self):
        # THE C1 FIX: an entry recording NO chip at all — just a storage rule (the real "odin3"
        # shape: its payload has no super image, so backfill can never give it a chip; it is a
        # permanent universal UFS wildcard) — must gate-pass (nothing conflicts) but `agreed` must be
        # 0 (vacuous). Storage agreement is corroborating, never chip-affirming: "we are both UFS" is
        # not evidence of ramdisk compatibility.
        fw = make_fw(self.root, "odin3-like", storage="ufs", match={})
        ok, _, agreed = FW.gate_check(fw, self._rp6())
        self.assertTrue(ok)
        self.assertEqual(agreed, 0)

    # --- board_platform outranks a soc SKU conflict (measured: live RP6 reports QCS8550) -----------
    def test_platform_agreement_outranks_a_soc_conflict(self):
        # A generic kalama build records soc=SM8550; the real RP6 reports soc=QCS8550. Same silicon
        # (QCS is the IoT SKU), and board_platform agrees on both sides -> must NOT reject.
        fw = self._fw(board_platform="kalama", soc="SM8550")
        ok, reason, agreed = FW.gate_check(fw, self._rp6(soc="QCS8550"))
        self.assertTrue(ok, f"platform agreed but the soc SKU rejected: {reason}")
        self.assertIsNone(reason)
        self.assertEqual(agreed, 1)     # platform affirmed; the conflicting soc adds nothing

    def test_soc_conflict_still_rejects_when_firmware_has_no_board_platform(self):
        # No platform to outrank it -> soc remains the fallback chip axis and must still reject.
        fw = self._fw(soc="SM8550")
        ok, reason, agreed = FW.gate_check(fw, self._rp6(soc="QCS8550"))
        self.assertFalse(ok)
        self.assertIn("QCS8550", reason)
        self.assertEqual(agreed, 0)

    def test_soc_conflict_still_rejects_when_device_reports_no_board_platform(self):
        fw = self._fw(board_platform="kalama", soc="SM8550")
        ok, reason, agreed = FW.gate_check(fw, self._rp6(board_platform="", soc="QCS8550"))
        self.assertFalse(ok)
        self.assertEqual(agreed, 0)

    def test_board_platform_conflict_still_rejects_regardless_of_soc(self):
        # A platform conflict is unconditional — an agreeing soc must not rescue it.
        fw = self._fw(board_platform="sun", soc="QCS8550")
        ok, reason, agreed = FW.gate_check(fw, self._rp6(soc="QCS8550"))
        self.assertFalse(ok)
        self.assertIn("kalama", reason)
        self.assertEqual(agreed, 0)

    def test_platform_and_soc_both_agree_still_affirms_twice(self):
        fw = self._fw(board_platform="kalama", soc="QCS8550")
        ok, _reason, agreed = FW.gate_check(fw, self._rp6(soc="QCS8550"))
        self.assertTrue(ok)
        self.assertEqual(agreed, 2)


# ---------------------------------------------------------------------------
# Task 5: logic_check() — brick-guard
# ---------------------------------------------------------------------------

class TestLogicCheck(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        self.root.mkdir(parents=True)
        self.fw = make_fw(self.root, "mangmi-air-x-mq66", device="AIR_X", flash="init_boot",
                          match={"serial_prefix": ["MQ66"], "device": "AIR_X"})

    def test_ok_when_consistent(self):
        ok, warns = FW.logic_check(self.fw, {"serial": "MQ66x", "device": "AIR_X",
                                             "flash_target": "init_boot_b"})
        self.assertTrue(ok)
        self.assertEqual(warns, [])

    def test_warns_on_partition_mismatch(self):
        ok, warns = FW.logic_check(self.fw, {"serial": "MQ66x", "device": "AIR_X",
                                             "flash_target": "boot_a"})
        self.assertFalse(ok)
        self.assertTrue(any("init_boot" in w and "boot" in w for w in warns))

    def test_warns_on_serial_mismatch_only(self):
        # Was 2 warnings; the device-inequality warning is deleted. A firmware's human label
        # ('Odin2 (kalama)') never equals a live ro.product.device, so that warning was always true
        # and never meaningful — which is exactly what trained operators to click through warnings.
        ok, warns = FW.logic_check(self.fw, {"serial": "MQ65x", "device": "Pocket_Max",
                                             "flash_target": "init_boot_b"})
        self.assertFalse(ok)
        self.assertEqual(len(warns), 1)
        self.assertIn("MQ65x", warns[0])

    def test_no_device_inequality_warning_on_proven_cross_brand_pair(self):
        # RP6 rooted from the Odin 2 build: proven to boot, must be SILENT.
        fw = make_fw(self.root, "ayn-odin2", device="odin2", flash="init_boot",
                     match={"device": "odin2", "board_platform": "kalama"})
        ok, warns = FW.logic_check(fw, {"serial": "RP6x", "device": "RP6",
                                        "flash_target": "init_boot_a"})
        self.assertTrue(ok)
        self.assertEqual(warns, [])


# ---------------------------------------------------------------------------
# Task 6: detect_build + ingest
# ---------------------------------------------------------------------------

class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def test_detect_build(self):
        src = fake_build(
            self.tmp,
            "MANGMI_Vex6115_FlatBuild_TurboX-C6115_xx.xx_la2.0.l.user.20260507.165105")
        d = FW.detect_build(src)
        self.assertEqual(d["storage"], "emmc")
        self.assertEqual(d["flash_target"], "init_boot")
        self.assertEqual(d["version"], "20260507-165105")
        self.assertEqual(d["device"], "AIR_X")
        self.assertEqual(d["dev_code"], "MQ66")
        self.assertEqual(d["os_version"], "1.1.6")

    def test_ingest_creates_version_and_history(self):
        src = fake_build(
            self.tmp,
            "MANGMI_Vex6115_FlatBuild_TurboX-C6115_xx.xx_la2.0.l.user.20260507.165105")
        fw = FW.ingest(src, self.root, firmware_id="mangmi-air-x-mq66",
                       match={"serial_prefix": ["MQ66"], "device": "AIR_X"})
        self.assertEqual(fw.current(), "20260507-165105")
        self.assertTrue((fw.payload_dir() / "emmc" / "super_1.img").is_file())
        self.assertEqual(len(fw.meta["history"]), 1)
        self.assertEqual(fw.flash_target, "init_boot")

    def test_ingest_idempotent_same_version(self):
        src = fake_build(self.tmp, "MANGMI_x_la2.0.l.user.20260507.165105")
        FW.ingest(src, self.root, firmware_id="fw")
        fw = FW.ingest(src, self.root, firmware_id="fw")   # no-op re-ingest
        self.assertEqual(len(fw.meta["history"]), 1)

    def test_ingest_second_version_bumps_current_keeps_old(self):
        FW.ingest(fake_build(self.tmp, "a_la2.0.l.user.20260506.000000"),
                  self.root, firmware_id="fw")
        fw = FW.ingest(fake_build(self.tmp, "b_la2.0.l.user.20260507.000000"),
                       self.root, firmware_id="fw")
        self.assertEqual(fw.current(), "20260507-000000")
        self.assertEqual(sorted(fw.versions()), ["20260506-000000", "20260507-000000"])
        self.assertEqual(len(fw.meta["history"]), 2)

    def test_ingest_wrong_device_guard(self):
        FW.ingest(fake_build(self.tmp, "a_la2.0.l.user.20260507.000000", device="AIR_X"),
                  self.root, firmware_id="fw")
        with self.assertRaises(ValueError):
            FW.ingest(fake_build(self.tmp, "b_la2.0.l.user.20260508.000000", device="Pocket_Max"),
                      self.root, firmware_id="fw")

    def test_ingest_no_label_raises_value_error(self):
        """Brick-guard: no boot/init_boot rawprogram label → ValueError."""
        d = pathlib.Path(self.tmp) / "no_label_build"
        p = d / "emmc"
        p.mkdir(parents=True)
        # rawprogram XML exists but contains only unrelated labels (no boot or init_boot)
        (p / "rawprogram1.xml").write_text('<data><program label="persist" /></data>')
        with self.assertRaises(ValueError):
            FW.ingest(d, self.root, firmware_id="fw-no-label")

    def test_ingest_seeds_device_match_from_detection(self):
        """A GUI ingest passes no match → ingest must seed match.device from the detected device so the
        firmware auto-matches instead of staying '(no match)'."""
        src = fake_build(self.tmp, "x_la2.0.l.user.20260507.000000", device="AIR_X")
        fw = FW.ingest(src, self.root, firmware_id="fw")
        self.assertEqual(fw.match_rules().get("device"), "AIR_X")

    def test_ingest_merges_caller_serial_prefix_with_detected_device(self):
        """Caller's serial_prefix (the MQ65/MQ66 discriminator) is kept AND device filled from detection."""
        src = fake_build(self.tmp, "y_la2.0.l.user.20260507.000000", device="AIR_X")
        fw = FW.ingest(src, self.root, firmware_id="fw2", match={"serial_prefix": ["MQ66"]})
        self.assertEqual(fw.match_rules().get("serial_prefix"), ["MQ66"])
        self.assertEqual(fw.match_rules().get("device"), "AIR_X")

    def test_detect_build_extracts_gate_fields(self):
        src = fake_build(self.tmp, "b-20260507.165105", board_platform="kalama",
                         soc="SM8550", android="13")
        info = FW.detect_build(src)
        self.assertEqual(info["board_platform"], "kalama")
        self.assertEqual(info["soc"], "SM8550")
        self.assertEqual(info["android_release"], "13")

    def test_detect_build_undetectable_gate_fields_are_empty(self):
        # A build whose props don't grep out must yield '' — the legacy entry stays legacy, no raise.
        src = fake_build(self.tmp, "c-20260507.165105", board_platform="", soc="", android="")
        info = FW.detect_build(src)
        self.assertEqual(info["board_platform"], "")
        self.assertEqual(info["soc"], "")
        self.assertEqual(info["android_release"], "")

    def test_ingest_seeds_gate_fields_with_no_caller_input(self):
        # The zero-knowledge operator path: ingest a build, its chip rules populate themselves.
        src = fake_build(self.tmp, "odin2-20260507.165105", board_platform="kalama",
                         soc="SM8550", android="13")
        fw = FW.ingest(src, self.root, firmware_id="ayn-odin2")
        r = fw.match_rules()
        self.assertEqual(r["board_platform"], "kalama")
        self.assertEqual(r["soc"], "SM8550")
        self.assertEqual(r["android_release"], "13")

    def test_ingest_undetectable_chip_leaves_entry_legacy_and_does_not_raise(self):
        src = fake_build(self.tmp, "legacy-20260507.165105", board_platform="", soc="", android="")
        fw = FW.ingest(src, self.root, firmware_id="legacy-fw")
        r = fw.match_rules()
        self.assertNotIn("board_platform", r)
        self.assertNotIn("soc", r)
        self.assertNotIn("android_release", r)

    def test_ingest_does_not_clobber_caller_supplied_match_rules(self):
        src = fake_build(self.tmp, "odin2b-20260507.165105", board_platform="kalama",
                         soc="SM8550", android="13")
        fw = FW.ingest(src, self.root, firmware_id="ayn-odin2b",
                       match={"serial_prefix": ["AYN"]})
        r = fw.match_rules()
        self.assertEqual(r["serial_prefix"], ["AYN"])       # caller's rule survives
        self.assertEqual(r["board_platform"], "kalama")     # detection fills the rest

    def test_ingest_caller_device_and_chip_rules_take_precedence_over_detection(self):
        """Mutation test: when caller supplies device/board_platform/soc/android_release that DIFFER from
        detected values, the caller's values must survive (not be clobbered by detection). This guards
        against a regression where removing the 'and not m.get(key)' clause would make detection always
        overwrite the caller — all existing tests would still pass because the test caller never supplies
        these keys, so caller-precedence would never be exercised."""
        # Build detects: device=AIR_X, board_platform=kalama, soc=SM8550, android=13
        src = fake_build(self.tmp, "test-caller-override-20260507.000000",
                         device="AIR_X", board_platform="kalama", soc="SM8550", android="13")
        # But caller explicitly passes DIFFERENT values for device and board_platform
        fw = FW.ingest(src, self.root, firmware_id="test-caller-override",
                       match={"device": "Pocket_Max", "board_platform": "bengal", "serial_prefix": ["TEST"]})
        r = fw.match_rules()
        # Caller's device and board_platform MUST survive (not be overwritten by detection)
        self.assertEqual(r["device"], "Pocket_Max",
                        "Caller-supplied device should not be clobbered by detection")
        self.assertEqual(r["board_platform"], "bengal",
                        "Caller-supplied board_platform should not be clobbered by detection")
        # Caller didn't supply soc/android_release, so detection should fill them in
        self.assertEqual(r["soc"], "SM8550")
        self.assertEqual(r["android_release"], "13")
        # Caller's serial_prefix should also survive
        self.assertEqual(r["serial_prefix"], ["TEST"])


# ---------------------------------------------------------------------------
# Task 7 (adapted): resolve() — uses fw-local get/set_device_firmware
# ---------------------------------------------------------------------------

class TestResolve(unittest.TestCase):
    def setUp(self):
        # CAS_CONFIG isolation: save+restore (not a bare pop) — see TestDeviceFirmware.setUp above.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)
        make_fw(self.root, "mangmi-air-x-mq66",
                match={"serial_prefix": ["MQ66"], "device": "AIR_X"})
        make_fw(self.root, "mangmi-air-x-mq65",
                match={"serial_prefix": ["MQ65"], "device": "AIR_X"})

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    def _idn(self, serial):
        return {"serial": serial, "device": "AIR_X", "soc": "SM6115",
                "flash_target": "init_boot_b"}

    def test_auto_suggests_and_remembers(self):
        r = FW.resolve("MQ66x", self._idn("MQ66x"), self.root)
        self.assertEqual(r["firmware_id"], "mangmi-air-x-mq66")
        self.assertEqual(r["suggested"], "mangmi-air-x-mq66")
        self.assertFalse(r["manual"])
        self.assertTrue(r["ok"])
        self.assertEqual(FW.get_device_firmware()["MQ66x"]["firmware_id"], "mangmi-air-x-mq66")

    def test_manual_override_wins(self):
        FW.set_device_firmware("MQ66x", "mangmi-air-x-mq65", manual=True)
        r = FW.resolve("MQ66x", self._idn("MQ66x"), self.root)
        self.assertEqual(r["firmware_id"], "mangmi-air-x-mq65")
        self.assertTrue(r["manual"])
        self.assertFalse(r["ok"])  # logic_check warns: MQ66 serial vs MQ65 firmware

    def test_default_kit_sentinel_pins_to_bundled_init_boot(self):
        # Assigning the "(default kit)" sentinel pins the unit to the bundled DEFAULT init_boot: no Firmware
        # object (so root falls back to the kit image), no auto-match, no warning, sticky.
        FW.set_device_firmware("ZZ", FW.DEFAULT_FW_ID, manual=True)
        r = FW.resolve("ZZ", {"serial": "ZZ", "device": "OTHER"}, self.root)
        self.assertEqual(r["firmware_id"], FW.DEFAULT_FW_ID)
        self.assertIsNone(r["firmware"])     # no build -> root_all keeps the DEFAULT kit init_boot
        self.assertTrue(r["ok"])             # NOT "(no match)" / not an error
        self.assertTrue(r["manual"])
        self.assertFalse(r["warnings"])
        # and it must NOT get auto-reassigned away
        self.assertEqual(FW.get_device_firmware()["ZZ"]["firmware_id"], FW.DEFAULT_FW_ID)

    def test_pinned_version_used(self):
        FW.set_device_firmware("S", "mangmi-air-x-mq66", version="20260101-000000", manual=True)
        r = FW.resolve("S", self._idn("S"), self.root)
        self.assertEqual(r["version"], "20260101-000000")

    def test_no_match(self):
        r = FW.resolve("ZZ", {"serial": "ZZ", "device": "OTHER"}, self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertFalse(r["ok"])


# ---------------------------------------------------------------------------
# I1: resolve() must re-gate a cached NON-MANUAL assignment before trusting it
# ---------------------------------------------------------------------------

class TestResolveRegatesCachedAssignment(unittest.TestCase):
    """A cached assignment with manual=False was an AUTO-SUGGESTION cached earlier — possibly by the
    OLD flat-score code (the very "stale serial_prefix outvotes chip" bug this branch fixes). Those
    stale suggestions live in the operator's cas-config.json and survive the C1/match() fix untouched
    unless resolve() itself re-validates them. Proven bug: gate_check(odin3-sun, RP6) rejects on chip,
    match(RP6) correctly picks rp6-kalama, yet resolve(RP6) returned the STALE cached odin3-sun with
    ok=True warnings=[] — a confident green light to brick the device."""

    def setUp(self):
        self._saved_cas_config = os.environ.get("CAS_CONFIG")
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)
        make_fw(self.root, "odin3-sun", device="odin3", storage="ufs",
                match={"board_platform": "sun"})
        make_fw(self.root, "rp6-kalama", device="RP6", storage="ufs",
                match={"device": "RP6", "board_platform": "kalama"})

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    def _rp6(self, serial="RP6x"):
        return {"serial": serial, "device": "RP6", "brand": "Retroid",
                "board_platform": "kalama", "soc": "SM8550", "android_release": "13",
                "bootdevice": "1d84000.ufshc"}

    def test_stale_non_manual_assignment_failing_gate_is_discarded_and_rematched(self):
        FW.set_device_firmware("RP6x", "odin3-sun", manual=False)   # stale auto-suggestion, wrong chip
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertEqual(r["firmware_id"], "rp6-kalama")
        self.assertFalse(r["manual"])
        self.assertTrue(r["ok"])
        # the cache itself is corrected too, not left pointing at the wrong-chip build
        self.assertEqual(FW.get_device_firmware()["RP6x"]["firmware_id"], "rp6-kalama")

    def test_manual_assignment_failing_gate_is_retained_not_dropped(self):
        FW.set_device_firmware("RP6x", "odin3-sun", manual=True)    # explicit operator override
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertEqual(r["firmware_id"], "odin3-sun")
        self.assertTrue(r["manual"])

    def test_non_manual_assignment_still_passing_gate_is_kept_no_churn(self):
        FW.set_device_firmware("RP6x", "rp6-kalama", manual=False)
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertEqual(r["firmware_id"], "rp6-kalama")
        self.assertFalse(r["manual"])
        self.assertTrue(r["ok"])


# ---------------------------------------------------------------------------
# Task 7b: log_event() — assignment/update audit jsonl
# ---------------------------------------------------------------------------

class TestAuditLog(unittest.TestCase):
    def setUp(self):
        # CAS_CONFIG isolation: save+restore (not a bare pop) — see TestDeviceFirmware.setUp above.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        os.environ["CAS_PROFILES"] = self.tmp

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config
        os.environ.pop("CAS_PROFILES", None)

    def test_log_event_appends_jsonl(self):
        FW.log_event("S1", "fw", "v1", "assign", True, when="2026-06-27 12:00")
        FW.log_event("S2", "fw2", "v2", "update", False, when="2026-06-27 12:01")
        p = pathlib.Path(C.history_dir()) / C.history_filename("firmware-history")
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["serial"], "S1")
        self.assertEqual(lines[1]["action"], "update")


class TestCasConfigIsolationRestoresNotPops(unittest.TestCase):
    """Regression guard: TestDeviceFirmware / TestResolve / TestAuditLog's tearDown() used to
    unconditionally `os.environ.pop("CAS_CONFIG", None)` instead of restoring whatever CAS_CONFIG was
    before their own setUp() overrode it — leaving it UNSET for whatever test runs next in the module
    (re-opening the bug commit 1492ce8 closed: a test without CAS_CONFIG pointed at a temp file falls
    straight through to the operator's real, gitignored cas-config.json). Drives each class's
    setUp()/tearDown() directly and proves CAS_CONFIG comes back to its PRE-setUp value, not gone."""

    def _check(self, test_case_cls):
        before = os.environ.get("CAS_CONFIG")
        tc = test_case_cls.__new__(test_case_cls)      # bypass __init__ (no test method needed)
        try:
            tc.setUp()
            try:
                self.assertIn("CAS_CONFIG", os.environ)    # setUp DOES override it — expected
            finally:
                tc.tearDown()
            got = os.environ.get("CAS_CONFIG")
        finally:
            # Force CAS_CONFIG back to `before` ourselves regardless of what tc.tearDown() actually did —
            # otherwise a FAILING case here leaks its own contamination onto whatever test runs next
            # (silently re-committing the very bug this guard exists to catch).
            if before is None:
                os.environ.pop("CAS_CONFIG", None)
            else:
                os.environ["CAS_CONFIG"] = before
        self.assertEqual(got, before,
                         f"{test_case_cls.__name__}.tearDown() must RESTORE CAS_CONFIG, not just pop it")

    def test_device_firmware_restores_cas_config(self):
        self._check(TestDeviceFirmware)

    def test_resolve_restores_cas_config(self):
        self._check(TestResolve)

    def test_audit_log_restores_cas_config(self):
        self._check(TestAuditLog)


def fake_edl_build(tmp, name):
    """A Firehose/EDL device-firmware build: bundled QSaharaServer/fh_loader + emmc/{prog_firehose,
    rawprogram with init_boot_a/_b geometry, init_boot.img}."""
    d = pathlib.Path(tmp) / name
    d.mkdir(parents=True)
    (d / "QSaharaServer").write_text("#!/bin/sh\n")
    (d / "fh_loader").write_text("#!/bin/sh\n")
    p = d / "emmc"
    p.mkdir(parents=True)
    (p / "prog_firehose_ddr.elf").write_bytes(b"\x7fELF")
    (p / "init_boot.img").write_bytes(b"ANDROID!" + b"\0" * 64)
    (p / "rawprogram1.xml").write_text(
        '<data>'
        '<program SECTOR_SIZE_IN_BYTES="512" filename="init_boot.img" label="init_boot_a" '
        'num_partition_sectors="16384" physical_partition_number="0" '
        'start_byte_hex="0x1f5802000" start_sector="16433168" />'
        '<program SECTOR_SIZE_IN_BYTES="512" filename="" label="init_boot_b" '
        'num_partition_sectors="16384" physical_partition_number="0" '
        'start_byte_hex="0x1f6002000" start_sector="16449552" />'
        '</data>')
    (p / "super_1.img").write_text("ro.product.system.device=AIR_X\nro.mangmi.dev.code=MQ66\n")
    return d


class TestFlashMethod(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def test_ingest_detects_edl_and_exposes_tools_and_geometry(self):
        src = fake_edl_build(self.tmp, "MANGMI_x_la2.0.l.user.20260507.165105")
        fw = FW.ingest(src, self.root, firmware_id="air-x")
        self.assertEqual(fw.flash_method, "edl")
        tools = fw.edl_tools()
        self.assertIsNotNone(tools)
        self.assertTrue(str(tools[0]).endswith("QSaharaServer"))
        g = fw.init_boot_geometry("_b")
        self.assertEqual(g["start_sector"], "16449552")
        self.assertEqual(g["partition"], "0")
        self.assertEqual(g["sector_size"], "512")

    def test_ingest_non_firehose_is_fastboot(self):
        src = fake_build(self.tmp, "Retroid_la2.0.l.user.20260507.000000")   # no QSahara/fh_loader/firehose
        fw = FW.ingest(src, self.root, firmware_id="rp")
        self.assertEqual(fw.flash_method, "fastboot")

    def test_flasher_for_firmware_picks_edl_vs_fastboot(self):
        from cas import provision as PV
        # This test verifies the SELECTION logic (edl vs fastboot), so the EDL build must carry tools the
        # CURRENT host can run — otherwise flasher_for_firmware correctly returns (None, reason) on Windows,
        # where a Linux ELF can't execute (see test_edl_tools_prefers_windows_exe / the os.name=='nt' guard).
        # Add the .exe host tools alongside the ELF so the build is runnable on every runner; the ELF-only
        # Windows honest-error path is covered separately.
        edl_src = fake_edl_build(self.tmp, "MANGMI_la2.0.l.user.20260507.165105")
        (edl_src / "QSaharaServer.exe").write_bytes(b"MZ")
        (edl_src / "fh_loader.exe").write_bytes(b"MZ")
        edl_fw = FW.ingest(edl_src, self.root, firmware_id="air-x")
        flasher, reason = PV.flasher_for_firmware(edl_fw, fastboot=None, slot="_b",
                                                  runner=lambda *a, **k: (0, "", ""))
        self.assertIsNotNone(flasher)
        self.assertIsNone(reason)
        fb_fw = FW.ingest(fake_build(self.tmp, "Retroid_la2.0.l.user.20260507.000000"),
                          self.root, firmware_id="rp")
        flasher2, reason2 = PV.flasher_for_firmware(fb_fw, fastboot="FBOBJ", slot="_a")
        self.assertIsNotNone(flasher2)
        self.assertIsNone(reason2)

    def test_edl_tools_prefers_windows_exe(self):
        """On Windows, edl_tools() returns the .exe host tools when present (the Linux ELF can't run there
        — subprocess raises WinError 193). On POSIX it stays with the extensionless ELF."""
        src = fake_edl_build(self.tmp, "MANGMI_win_la2.0.l.user.20260507.165105")
        (src / "QSaharaServer.exe").write_bytes(b"MZ")     # a real Windows PE marker
        (src / "fh_loader.exe").write_bytes(b"MZ")
        fw = FW.ingest(src, self.root, firmware_id="air-x")
        with mock.patch.object(FW.os, "name", "nt"):
            q, f, _ = fw.edl_tools()
            self.assertTrue(str(q).endswith("QSaharaServer.exe"))
            self.assertTrue(str(f).endswith("fh_loader.exe"))
        with mock.patch.object(FW.os, "name", "posix"):
            q, f, _ = fw.edl_tools()
            self.assertTrue(str(q).endswith("QSaharaServer"))
            self.assertFalse(str(q).endswith(".exe"))

    def test_flasher_windows_rejects_linux_only_edl_tools(self):
        """A Windows bench with only the Linux QSaharaServer/fh_loader must fail fast with a clear,
        honest reason (name the missing .exe / QPST) — NOT blame the QDLoader driver and NOT strand the
        unit in EDL. This is the MQ66 'WinError 193 %1 is not a valid Win32 application' case."""
        from cas import provision as PV
        edl_fw = FW.ingest(fake_edl_build(self.tmp, "MANGMI_linux_la2.0.l.user.20260507.165105"),
                           self.root, firmware_id="air-x")
        with mock.patch.object(FW.os, "name", "nt"):
            flasher, reason = PV.flasher_for_firmware(edl_fw, fastboot=None, slot="_b",
                                                      runner=lambda *a, **k: (0, "", ""))
        self.assertIsNone(flasher)
        self.assertIsNotNone(reason)
        self.assertIn(".exe", reason)


class TestDefaultKitFirmware(unittest.TestCase):
    """'(default kit)' used to resolve to a HARD-CODED init_boot path (odin2_20231201) that is
    gitignored and not bundled — so in a release/fresh checkout Root failed with 'missing
    init_boot.img'. It can now be UNIFIED with the firmware library: designate a library build as the
    default kit and a device pinned to '(default kit)' flashes THAT build's init_boot (present),
    instead of the un-shipped path. This is the kalama (RP6 / Odin2 Mini) fix."""

    def _lib_with_odin2(self, root):
        """A firmware library holding an odin2 build with a real init_boot.img in its payload."""
        fw = make_fw(root, "odin2-default", device="Odin2", flash="init_boot",
                     match={"serial_prefix": ["ODIN2"]})
        (fw.payload_dir() / "init_boot.img").write_bytes(b"ANDROID!ramdisk")   # what stock_boot_image globs
        return fw

    def test_designation_round_trips_and_clears(self):
        from cas import config
        config.set_default_kit_firmware("odin2-default")
        self.assertEqual(config.default_kit_firmware(), "odin2-default")
        config.set_default_kit_firmware(None)
        self.assertIsNone(config.default_kit_firmware())

    def test_default_kit_firmware_resolves_the_designated_library_build(self):
        with tempfile.TemporaryDirectory() as td:
            from cas import config
            self._lib_with_odin2(td)
            config.set_default_kit_firmware("odin2-default")
            fw = FW.default_kit_firmware(td)
            self.assertIsNotNone(fw)
            self.assertEqual(fw.id, "odin2-default")

    def test_none_when_unset_or_missing_from_library(self):
        with tempfile.TemporaryDirectory() as td:
            from cas import config
            self.assertIsNone(FW.default_kit_firmware(td))          # unset
            config.set_default_kit_firmware("not-in-library")
            self.assertIsNone(FW.default_kit_firmware(td))          # designated id not present → None

    def test_resolve_maps_default_kit_to_the_designated_build_with_a_flashable_image(self):
        with tempfile.TemporaryDirectory() as td:
            from cas import config
            self._lib_with_odin2(td)
            config.set_default_kit_firmware("odin2-default")
            FW.set_device_firmware("ODIN2ABC", FW.DEFAULT_FW_ID, manual=True)
            # device codename ('kalama') deliberately DIFFERS from the firmware's human device label
            # ('Odin2') — the default kit must stay FRICTIONLESS (no logic_check → no false warning),
            # or every real Odin2 would carry a permanent ⚠ (its label never equals ro.product.device).
            r = FW.resolve("ODIN2ABC", {"serial": "ODIN2ABC", "device": "kalama"}, td)
            self.assertEqual(r["firmware_id"], FW.DEFAULT_FW_ID)    # still shows as the default kit
            self.assertIsNotNone(r["firmware"])                    # ...but now backed by a real build
            self.assertTrue(str(r["firmware"].stock_boot_image()).endswith("init_boot.img"))
            self.assertTrue(r["ok"])                               # frictionless: no warning
            self.assertEqual(r["warnings"], [])

    def test_resolve_default_kit_without_a_designation_keeps_the_old_none_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            FW.set_device_firmware("ODIN2ABC", FW.DEFAULT_FW_ID, manual=True)
            r = FW.resolve("ODIN2ABC", {"serial": "ODIN2ABC"}, td)
            self.assertEqual(r["firmware_id"], FW.DEFAULT_FW_ID)
            self.assertIsNone(r["firmware"])                       # no designation → falls back as before


# ---------------------------------------------------------------------------
# Task 8: _no_match_reasons() — (no match) explains itself
# ---------------------------------------------------------------------------

class TestNoMatchReasons(unittest.TestCase):
    def setUp(self):
        self._saved_cas_config = os.environ.get("CAS_CONFIG")
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config

    def _rp6(self):
        return {"serial": "RP6x", "device": "RP6", "brand": "Retroid", "board_platform": "kalama",
                "soc": "SM8550", "android_release": "13", "bootdevice": "1d84000.ufshc"}

    def test_reason_names_the_chip_when_all_entries_were_rejected(self):
        make_fw(self.root, "ayn-odin3", device="odin3", storage="ufs",
                match={"board_platform": "sun"})
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertTrue(any("kalama" in w for w in r["warnings"]),
                        f"expected the chip named in {r['warnings']}")

    def test_reason_says_run_backfill_when_entries_have_no_chip(self):
        # Chip-less but SCANNABLE (payload has a super image) — backfill can actually fill these, so
        # the hint must still point there. (Pinned by Task 3: an empty payload is the unscannable case
        # and must NOT be conflated with this one — see test_does_not_recommend_backfill_* below.)
        fw_a = make_fw(self.root, "legacy-a", device="x", storage="", match={})
        (fw_a.payload_dir() / "super_1.img").write_bytes(b"x")
        fw_b = make_fw(self.root, "legacy-b", device="y", storage="", match={})
        (fw_b.payload_dir() / "super_1.img").write_bytes(b"x")
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertTrue(any("backfill" in w for w in r["warnings"]),
                        f"expected a backfill hint in {r['warnings']}")

    def test_empty_library_says_neither(self):
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertTrue(any("no match" in w for w in r["warnings"]))

    # --- I2: use the per-axis reason instead of throwing it away -----------------------------------

    def test_android_rejection_does_not_tell_operator_to_ingest_a_chip_they_already_have(self):
        # The build for this chip EXISTS (kalama, matches) and was rejected on ANDROID. "ingest a
        # build for it" is impossible advice — the operator already owns the build; the mismatch is
        # elsewhere. The message must name android specifically instead.
        make_fw(self.root, "ayn-odin2-old-android", device="odin2", storage="ufs",
                match={"board_platform": "kalama", "android_release": "12"})
        r = FW.resolve("RP6x", self._rp6(), self.root)   # rp6 android_release="13"
        self.assertIsNone(r["firmware_id"])
        self.assertFalse(any("ingest a build for it" in w for w in r["warnings"]),
                         f"must not tell the operator to ingest a chip build they already have: {r['warnings']}")
        self.assertTrue(any("android" in w for w in r["warnings"]),
                        f"expected android named specifically in {r['warnings']}")

    def test_storage_rejection_does_not_tell_operator_to_ingest_a_chip_they_already_have(self):
        make_fw(self.root, "ayn-odin2-emmc", device="odin2", storage="emmc",
                match={"board_platform": "kalama"})
        r = FW.resolve("RP6x", self._rp6(), self.root)   # rp6 bootdevice is ufs
        self.assertIsNone(r["firmware_id"])
        self.assertFalse(any("ingest a build for it" in w for w in r["warnings"]),
                         f"must not tell the operator to ingest a chip build they already have: {r['warnings']}")
        self.assertTrue(any("storage" in w for w in r["warnings"]),
                        f"expected storage named specifically in {r['warnings']}")

    def test_chip_rejection_still_says_ingest_a_build_for_it(self):
        # Unchanged behavior for a genuine chip miss — pinned alongside the new axes so a future edit
        # can't silently drop the original (correct) chip-rejection message.
        make_fw(self.root, "ayn-odin3", device="odin3", storage="ufs",
                match={"board_platform": "sun"})
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertTrue(any("ingest a build for it" in w and "kalama" in w for w in r["warnings"]),
                        f"expected the chip-miss ingest hint in {r['warnings']}")

    def test_soc_only_entry_is_not_reported_as_records_no_chip(self):
        # gate_check() treats board_platform AND soc as chip axes; _no_match_reasons must too. An
        # entry recording only a `soc` rule is not "legacy"/"records no chip" just because the LIVE
        # device didn't report soc (so the axis abstained rather than compared).
        #
        # "records no chip" now has TWO phrasings (scannable -> backfill hint, unscannable -> set
        # --chip hint) — this fixture's payload is empty (make_fw's default), so a board_platform-only
        # chip-less check would misfile it as UNSCANNABLE, whose message contains no "backfill"
        # substring. Asserting only the absence of "backfill" would pass on that wrong output. Assert
        # on the concept instead: no "record no chip" advice of ANY kind, since a soc rule means the
        # entry isn't chip-less at all. With no other firmware present and this entry gate-passing,
        # the only correct outcome is the generic fallback.
        make_fw(self.root, "soc-only", device="x", storage="", match={"soc": "SM7999"})
        idn = self._rp6()
        idn = dict(idn)
        idn.pop("soc", None)   # device doesn't report soc -> soc axis abstains, agreed stays 0
        r = FW.resolve("RP6x", idn, self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertFalse(any("record no chip" in w for w in r["warnings"]),
                         f"a soc-recording entry must not be reported as 'records no chip' in any "
                         f"phrasing (backfill or set --chip): {r['warnings']}")
        self.assertEqual(r["warnings"], ["no match — select manually"],
                         f"soc-only entry gate-passes with agreed=0 and isn't chip-less, so no "
                         f"chip-axis advice should fire at all: {r['warnings']}")

    # --- Task 3: don't recommend backfill for an entry it can never fix --------------------------

    def _bare_legacy(self, fid):
        """Chip-less AND no super image: backfill can never help it."""
        fw = make_fw(self.root, fid, storage="", match={})
        (fw.payload_dir() / "init_boot.img").write_bytes(b"x")
        return fw

    def test_does_not_recommend_backfill_when_the_entry_has_no_super_image(self):
        # The measured dead end: "run backfill" -> 91 min -> "0 backfilled" -> still (no match).
        self._bare_legacy("bare-legacy")
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertIsNone(r["firmware_id"])
        self.assertFalse(any("backfill" in w for w in r["warnings"]),
                         f"recommended backfill for an entry it can never fix: {r['warnings']}")
        self.assertTrue(any("set --chip" in w for w in r["warnings"]),
                        f"expected a 'set --chip' hint in {r['warnings']}")

    def test_still_recommends_backfill_when_a_chip_less_entry_has_a_super_image(self):
        src = fake_build(self.tmp, "scan-20260507.165105", storage="ufs", board_platform="kalama",
                         soc="QCS8550", android="13")
        FW.ingest(src, self.root, firmware_id="scannable")
        meta = FW._read_json(self.root / "scannable" / "meta.json")
        meta["match"] = {}
        FW._write_json(self.root / "scannable" / "meta.json", meta)
        r = FW.resolve("RP6x", self._rp6(), self.root)
        self.assertTrue(any("backfill" in w for w in r["warnings"]),
                        f"expected a backfill hint in {r['warnings']}")


# ---------------------------------------------------------------------------
# Task 9: set_gate_fields() — escape hatch for chip/android/storage gate fields
# ---------------------------------------------------------------------------

class TestSetGateFields(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp()) / "_firmware"
        self.root.mkdir(parents=True)
        make_fw(self.root, "legacy-fw", device="x", storage="", match={})

    def test_set_writes_gate_fields(self):
        fw = FW.set_gate_fields("legacy-fw", self.root, chip="kalama", soc="SM8550",
                                android="13", storage="ufs")
        r = fw.match_rules()
        self.assertEqual(r["board_platform"], "kalama")
        self.assertEqual(r["soc"], "SM8550")
        self.assertEqual(r["android_release"], "13")
        self.assertEqual(fw.storage, "ufs")

    def test_set_is_idempotent(self):
        FW.set_gate_fields("legacy-fw", self.root, chip="kalama")
        fw = FW.set_gate_fields("legacy-fw", self.root, chip="kalama")
        self.assertEqual(fw.match_rules()["board_platform"], "kalama")

    def test_set_only_touches_named_fields(self):
        FW.set_gate_fields("legacy-fw", self.root, chip="kalama")
        fw = FW.set_gate_fields("legacy-fw", self.root, android="13")
        self.assertEqual(fw.match_rules()["board_platform"], "kalama")   # survives
        self.assertEqual(fw.match_rules()["android_release"], "13")

    def test_set_unknown_id_raises(self):
        with self.assertRaises(ValueError):
            FW.set_gate_fields("nope", self.root, chip="kalama")


# ---------------------------------------------------------------------------
# Task 9 (review fix): main() 'set' subcommand — argparse dispatch is untested no longer
# ---------------------------------------------------------------------------

class TestMainSetSubcommand(unittest.TestCase):
    """Reviewer finding: main()'s dispatch had ZERO test coverage (no test called main() at all for
    any subcommand). Two things specifically must be pinned for `set`:
      1. `main(["set", ...])` actually works end-to-end through real argparse parsing and writes
         meta.json via the real CLI path (not just set_gate_fields() called directly).
      2. `set` dispatches BEFORE the ("show", "assign") elif branch, which constructs an Adb and
         talks to hardware over adb. If a future refactor lets 'set' fall into that branch (e.g. the
         device-touching branch becomes a catch-all), `set` would try to open an adb connection and
         hang/fail on a bench with no device plugged in — `set` must never require a device."""

    def setUp(self):
        # CAS_CONFIG isolation: save+restore (not a bare pop) — see TestDeviceFirmware.setUp above.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")
        self._saved_cas_profiles = os.environ.get("CAS_PROFILES")
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        os.environ["CAS_PROFILES"] = self.tmp   # pins firmware_root() to tmp/_firmware
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)
        make_fw(self.root, "legacy-fw", device="x", storage="", match={})

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config
        if self._saved_cas_profiles is None:
            os.environ.pop("CAS_PROFILES", None)
        else:
            os.environ["CAS_PROFILES"] = self._saved_cas_profiles

    def _meta(self):
        return FW._read_json(self.root / "legacy-fw" / "meta.json")

    def test_set_writes_gate_fields_through_real_cli(self):
        FW.main(["set", "legacy-fw", "--chip", "kalama", "--soc", "SM8550",
                 "--android", "13", "--storage", "ufs"])
        meta = self._meta()
        self.assertEqual(meta["match"]["board_platform"], "kalama")
        self.assertEqual(meta["match"]["soc"], "SM8550")
        self.assertEqual(meta["match"]["android_release"], "13")
        self.assertEqual(meta["storage"], "ufs")

    def test_set_partial_flags_leave_others_intact(self):
        FW.main(["set", "legacy-fw", "--chip", "kalama", "--storage", "ufs"])
        FW.main(["set", "legacy-fw", "--android", "13"])   # only android this time — no --chip/--storage
        meta = self._meta()
        self.assertEqual(meta["match"]["board_platform"], "kalama")   # survives the second call
        self.assertEqual(meta["storage"], "ufs")                      # survives the second call
        self.assertEqual(meta["match"]["android_release"], "13")

    def test_set_storage_rejects_invalid_choice(self):
        with self.assertRaises(SystemExit) as cm:
            FW.main(["set", "legacy-fw", "--storage", "nvme"])
        self.assertEqual(cm.exception.code, 2)

    def test_set_never_constructs_adb(self):
        # THE PIN: `set` must dispatch before the ("show","assign") branch, which does
        # `Adb(serial=..., adb=find_adb("adb"))`. Patch cas.adb.Adb to explode if instantiated — main()
        # imports it locally via `from .adb import Adb`, which is a live lookup on cas.adb at call time,
        # so patching the module attribute here is visible to that import. If `set` ever reached the
        # device-touching branch, this raises immediately instead of hanging on a bench with no device.
        with mock.patch("cas.adb.Adb", side_effect=AssertionError(
                "set must not construct Adb -- it dispatched into the device-touching branch")):
            FW.main(["set", "legacy-fw", "--chip", "kalama"])   # must complete without raising
        meta = self._meta()
        self.assertEqual(meta["match"]["board_platform"], "kalama")   # and the write still happened


# ---------------------------------------------------------------------------
# Task 10: backfill() — migration without a flag day
# ---------------------------------------------------------------------------

class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def _ingest_then_strip(self, fid, **kw):
        """Ingest a build, then strip its gate fields to simulate a pre-existing legacy entry."""
        src = fake_build(self.tmp, f"{fid}-20260507.165105", **kw)
        fw = FW.ingest(src, self.root, firmware_id=fid)
        meta = FW._read_json(fw.path / "meta.json")
        meta["match"] = {k: v for k, v in (meta.get("match") or {}).items()
                         if k not in ("board_platform", "soc", "android_release")}
        FW._write_json(fw.path / "meta.json", meta)
        return FW.Firmware(fw.path)

    def test_backfill_fills_gate_fields_from_the_payload(self):
        self._ingest_then_strip("ayn-odin2", board_platform="kalama", soc="SM8550", android="13")
        filled, _skipped = FW.backfill(self.root)
        self.assertEqual([fid for fid, _ in filled], ["ayn-odin2"])
        fw = FW.find("ayn-odin2", self.root)
        self.assertEqual(fw.match_rules()["board_platform"], "kalama")
        self.assertEqual(fw.match_rules()["android_release"], "13")

    def test_backfill_is_idempotent_and_reports_nothing_second_time(self):
        self._ingest_then_strip("ayn-odin2", board_platform="kalama", soc="SM8550", android="13")
        FW.backfill(self.root)
        filled, _skipped = FW.backfill(self.root)
        self.assertEqual(filled, [])

    def test_backfill_never_overwrites_an_operator_set_value(self):
        self._ingest_then_strip("ayn-odin2", board_platform="kalama", soc="SM8550", android="13")
        FW.set_gate_fields("ayn-odin2", self.root, chip="kalama-hand-set")
        FW.backfill(self.root)
        self.assertEqual(FW.find("ayn-odin2", self.root).match_rules()["board_platform"],
                         "kalama-hand-set")

    def test_backfill_skips_undetectable_entry_without_raising(self):
        self._ingest_then_strip("legacy", board_platform="", soc="", android="")
        filled, _skipped = FW.backfill(self.root)
        self.assertEqual(filled, [])

    def test_backfill_skips_corrupt_json_syntax_entry_without_clobbering_it(self):
        # Reviewer's literal example: meta.json with broken JSON syntax alongside a healthy entry.
        # _read_json() returns {} for this file, which would make match_rules() look like "everything
        # missing" -- backfill must not treat that as a legitimate target.
        self._ingest_then_strip("ayn-odin2", board_platform="kalama", soc="SM8550", android="13")
        corrupt_dir = self.root / "corrupt-fw"
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "meta.json").write_text("{not json")
        (corrupt_dir / "versions" / "20260101-000000" / "payload").mkdir(parents=True)
        before = (corrupt_dir / "meta.json").read_bytes()

        filled, _skipped = FW.backfill(self.root)

        self.assertEqual([fid for fid, _ in filled], ["ayn-odin2"])   # only the healthy entry
        after = (corrupt_dir / "meta.json").read_bytes()
        self.assertEqual(after, before)   # the corrupt file's BYTES are unchanged on disk

    def test_backfill_skips_non_dict_meta_without_crashing_or_clobbering_it(self):
        # A DIFFERENT shape of corruption: meta.json is syntactically valid JSON (json.loads succeeds,
        # so _read_json() does NOT fall back to {}) but parses to something other than a dict -- e.g.
        # a bare 'null'. Firmware.meta is then None, which is exactly as falsy as {} and just as much
        # "never a legitimate backfill target". Without the guard this doesn't just clobber the file --
        # fw.payload_dir() -> fw.current() -> None.get("current") raises AttributeError, which is not
        # caught anywhere in backfill(), so the WHOLE run aborts and even the healthy entry alongside it
        # never gets processed. This is the construction that actually discriminates: unlike the
        # syntax-broken-JSON case above (already saved by the pre-existing "no payload dir" check, since
        # a {} meta can never yield a "current" version), a None meta reaches fw.payload_dir() and blows
        # up there -- proving the new guard is load-bearing, not redundant.
        self._ingest_then_strip("ayn-odin2", board_platform="kalama", soc="SM8550", android="13")
        corrupt_dir = self.root / "corrupt-fw"
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "meta.json").write_text("null")
        (corrupt_dir / "versions" / "20260101-000000" / "payload").mkdir(parents=True)
        before = (corrupt_dir / "meta.json").read_bytes()

        filled, _skipped = FW.backfill(self.root)   # must not raise

        self.assertEqual([fid for fid, _ in filled], ["ayn-odin2"])
        after = (corrupt_dir / "meta.json").read_bytes()
        self.assertEqual(after, before)


class TestPayloadHasBuildImages(unittest.TestCase):
    """Distinguishes 'backfill can never help this' from 'backfill had nothing to add'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def test_true_when_payload_has_a_super_image(self):
        src = fake_build(self.tmp, "hasimg-20260507.165105")
        fw = FW.ingest(src, self.root, firmware_id="hasimg")
        self.assertTrue(FW._payload_has_build_images(fw))

    def test_false_when_payload_has_no_super_or_system_image(self):
        # The real shape of odin2-default / odin3 / retroid-pocket-5: a bare init_boot.img payload.
        fw = make_fw(self.root, "bare", storage="")
        (fw.payload_dir() / "init_boot.img").write_bytes(b"x")
        self.assertFalse(FW._payload_has_build_images(fw))

    def test_false_when_there_is_no_payload_at_all(self):
        d = self.root / "nopayload"
        (d / "versions").mkdir(parents=True)
        FW._write_json(d / "meta.json", {"id": "nopayload", "current": "v1", "match": {}})
        self.assertFalse(FW._payload_has_build_images(FW.Firmware(d)))


class TestBackfillReporting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)
        self.lines = []

    def _log(self, m):
        self.lines.append(str(m))

    def _bare(self, fid):
        """An entry with no super image — backfill can NEVER detect its chip."""
        fw = make_fw(self.root, fid, storage="")
        (fw.payload_dir() / "init_boot.img").write_bytes(b"x")
        return fw

    def test_returns_filled_and_skipped(self):
        src = fake_build(self.tmp, "good-20260507.165105", board_platform="kalama",
                         soc="QCS8550", android="13")
        FW.ingest(src, self.root, firmware_id="good")
        meta = FW._read_json(self.root / "good" / "meta.json")
        meta["match"] = {}
        FW._write_json(self.root / "good" / "meta.json", meta)
        self._bare("bare")

        filled, skipped = FW.backfill(self.root, log=self._log)
        self.assertEqual([fid for fid, _ in filled], ["good"])
        self.assertIn("bare", [fid for fid, _ in skipped])

    def test_no_super_image_skip_names_the_reason_and_the_fix(self):
        self._bare("bare")
        _filled, skipped = FW.backfill(self.root, log=self._log)
        reason = dict(skipped)["bare"]
        self.assertIn("no super", reason.lower())
        self.assertIn("set --chip", reason)

    def test_nothing_new_detected_is_reported_not_silent(self):
        src = fake_build(self.tmp, "done-20260507.165105", board_platform="kalama",
                         soc="QCS8550", android="13")
        FW.ingest(src, self.root, firmware_id="done")      # ingest already seeded every gate field
        _filled, skipped = FW.backfill(self.root, log=self._log)
        self.assertIn("done", [fid for fid, _ in skipped])
        self.assertIn("nothing new", dict(skipped)["done"].lower())

    def test_progress_is_emitted_for_every_entry_including_skipped_ones(self):
        # A skipped entry that prints nothing is exactly the bug being fixed.
        self._bare("bare")
        FW.backfill(self.root, log=self._log)
        self.assertTrue(any("bare" in l for l in self.lines),
                        f"no progress line mentioned the skipped entry: {self.lines}")
        self.assertTrue(any("1/1" in l or "[1/" in l for l in self.lines),
                        f"no [i/n] progress counter emitted: {self.lines}")

    def test_corrupt_meta_is_reported_not_silently_skipped(self):
        d = self.root / "corrupt"; d.mkdir()
        (d / "meta.json").write_text("null")
        _filled, skipped = FW.backfill(self.root, log=self._log)
        self.assertIn("corrupt", [fid for fid, _ in skipped])


# ---------------------------------------------------------------------------
# Task 10 (CLI): main() 'backfill' subcommand
# ---------------------------------------------------------------------------

class TestMainBackfillSubcommand(unittest.TestCase):
    """Mirrors TestMainSetSubcommand: `backfill` must work through real argparse dispatch, and — like
    `set` — must dispatch BEFORE the ("show", "assign") branch so it never constructs an Adb / needs a
    device plugged in."""

    def setUp(self):
        # CAS_CONFIG isolation: save+restore (not a bare pop) — see TestDeviceFirmware.setUp above.
        self._saved_cas_config = os.environ.get("CAS_CONFIG")
        self._saved_cas_profiles = os.environ.get("CAS_PROFILES")
        self.tmp = tempfile.mkdtemp()
        os.environ["CAS_CONFIG"] = os.path.join(self.tmp, "cas-config.json")
        os.environ["CAS_PROFILES"] = self.tmp   # pins firmware_root() to tmp/_firmware
        self.root = pathlib.Path(self.tmp) / "_firmware"
        self.root.mkdir(parents=True)

    def tearDown(self):
        if self._saved_cas_config is None:
            os.environ.pop("CAS_CONFIG", None)
        else:
            os.environ["CAS_CONFIG"] = self._saved_cas_config
        if self._saved_cas_profiles is None:
            os.environ.pop("CAS_PROFILES", None)
        else:
            os.environ["CAS_PROFILES"] = self._saved_cas_profiles

    def _ingest_then_strip(self, fid, **kw):
        src = fake_build(self.tmp, f"{fid}-20260507.165105", **kw)
        fw = FW.ingest(src, self.root, firmware_id=fid)
        meta = FW._read_json(fw.path / "meta.json")
        meta["match"] = {k: v for k, v in (meta.get("match") or {}).items()
                         if k not in ("board_platform", "soc", "android_release")}
        FW._write_json(fw.path / "meta.json", meta)
        return FW.Firmware(fw.path)

    def test_backfill_fills_gate_fields_through_real_cli(self):
        self._ingest_then_strip("ayn-odin2", board_platform="kalama", soc="SM8550", android="13")
        FW.main(["backfill"])
        meta = FW._read_json(self.root / "ayn-odin2" / "meta.json")
        self.assertEqual(meta["match"]["board_platform"], "kalama")
        self.assertEqual(meta["match"]["soc"], "SM8550")
        self.assertEqual(meta["match"]["android_release"], "13")

    def test_backfill_never_constructs_adb(self):
        # THE PIN: `backfill` must dispatch before the ("show","assign") branch, which does
        # `Adb(serial=..., adb=find_adb("adb"))`. Patch cas.adb.Adb to explode if instantiated — main()
        # imports it locally via `from .adb import Adb`, a live lookup on cas.adb at call time, so
        # patching the module attribute here is visible to that import. If `backfill` ever reached the
        # device-touching branch, this raises immediately instead of hanging on a bench with no device.
        self._ingest_then_strip("ayn-odin2", board_platform="kalama", soc="SM8550", android="13")
        with mock.patch("cas.adb.Adb", side_effect=AssertionError(
                "backfill must not construct Adb -- it dispatched into the device-touching branch")):
            FW.main(["backfill"])   # must complete without raising
        meta = FW._read_json(self.root / "ayn-odin2" / "meta.json")
        self.assertEqual(meta["match"]["board_platform"], "kalama")   # and the write still happened


class TestProvenPair(unittest.TestCase):
    """Task 11: log_proven_pair() records a (chip, android, storage, model, firmware_id, version)
    tuple that ACTUALLY BOOTED. EVIDENCE, NOT A GATE -- nothing reads this file to allow or block a
    flash. Mirrors log_event()'s jsonl pattern and its never-raises guarantee."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = os.environ.get("CAS_PROFILES")
        os.environ["CAS_PROFILES"] = self.tmp

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CAS_PROFILES", None)
        else:
            os.environ["CAS_PROFILES"] = self._saved

    def _idn(self):
        return {"serial": "RP6x", "model": "RP6", "board_platform": "kalama", "soc": "SM8550",
                "android_release": "13", "bootdevice": "1d84000.ufshc"}

    def test_logs_the_tuple(self):
        FW.log_proven_pair(self._idn(), "ayn-odin2", "20260507-165105", when="2026-07-16 10:00")
        p = pathlib.Path(C.history_dir()) / C.history_filename("firmware-proven")
        rec = json.loads(p.read_text().strip().splitlines()[-1])
        self.assertEqual(rec["chip"], "kalama")
        self.assertEqual(rec["android"], "13")
        self.assertEqual(rec["storage"], "ufs")
        self.assertEqual(rec["model"], "RP6")
        self.assertEqual(rec["firmware_id"], "ayn-odin2")
        self.assertEqual(rec["version"], "20260507-165105")

    def test_never_raises_on_a_bad_identity(self):
        FW.log_proven_pair(None, None, None)        # must not raise


if __name__ == "__main__":
    unittest.main()
