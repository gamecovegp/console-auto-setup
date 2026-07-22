# tests/test_lock_provenance.py
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cas import provision as PV


class TestProvenanceLabel(unittest.TestCase):
    def test_label_appends_provenance(self):
        self.assertEqual(PV._ota_detail("RP6 256", "captured"), "RP6 256 · ota:captured")

    def test_label_handles_empty_profile_name(self):
        self.assertEqual(PV._ota_detail("", "unverified"), "· ota:unverified")

    def test_waiver_label_for_ships_rooted(self):
        self.assertEqual(PV._ota_detail("RP5", "waived-ships-rooted"),
                         "RP5 · ota:waived-ships-rooted")


if __name__ == "__main__":
    unittest.main()
