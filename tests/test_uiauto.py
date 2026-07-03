import os, sys, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from cas import uiauto

MAGISK_PROMPT_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<hierarchy rotation=\"0\">"
    "<node index=\"0\" text=\"Superuser Request\" bounds=\"[0,100][1080,220]\" />"
    "<node index=\"1\" text=\"shell\" content-desc=\"\" bounds=\"[40,240][1040,360]\" />"
    "<node index=\"2\" text=\"Deny\" bounds=\"[0,900][540,1010]\" />"
    "<node index=\"3\" text=\"Grant\" bounds=\"[540,900][1080,1010]\" />"
    "</hierarchy>")


class FindControl(unittest.TestCase):
    def test_finds_grant_button_center(self):
        self.assertEqual(uiauto.find_control(MAGISK_PROMPT_XML, r"grant"), (810, 955))

    def test_case_insensitive(self):
        self.assertEqual(uiauto.find_control(MAGISK_PROMPT_XML, r"GRANT"), (810, 955))

    def test_no_match_returns_none(self):
        self.assertIsNone(uiauto.find_control(MAGISK_PROMPT_XML, r"nonexistent"))

    def test_empty_xml_returns_none(self):
        self.assertIsNone(uiauto.find_control("", r"grant"))

    def test_matches_content_desc_when_text_empty(self):
        xml = '<node text="" content-desc="Grant" bounds="[540,900][1080,1010]" />'
        self.assertEqual(uiauto.find_control(xml, r"grant"), (810, 955))

    def test_prefers_clickable_button_over_message_text(self):
        xml = ('<hierarchy>'
               '<node text="Grant shell root access?" clickable="false" bounds="[0,300][1080,420]" />'
               '<node text="GRANT" clickable="true" bounds="[540,900][1080,1010]" />'
               '</hierarchy>')
        # non-clickable message contains "grant" and comes FIRST, but the clickable button must win
        self.assertEqual(uiauto.find_control(xml, r"grant"), (810, 955))


if __name__ == "__main__":
    unittest.main()
