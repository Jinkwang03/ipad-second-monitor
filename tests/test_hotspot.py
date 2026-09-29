"""The laptop's own Wi-Fi (Mobile hotspot) helpers."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hotspot  # noqa: E402
import server  # noqa: E402


class HotspotTests(unittest.TestCase):
    def test_wifi_qr_code_text(self):
        self.assertEqual(hotspot.wifi_qr_text("DESKTOP-1 2382", "p4ss word"),
                         "WIFI:T:WPA;S:DESKTOP-1 2382;P:p4ss word;;")

    def test_wifi_qr_escapes_special_characters(self):
        self.assertEqual(hotspot.wifi_qr_text('my;net,"x":y', "a\\b"),
                         'WIFI:T:WPA;S:my\\;net\\,\\"x\\"\\:y;P:a\\\\b;;')

    def test_hotspot_options(self):
        self.assertTrue(server.parse_args(["--hotspot"]).hotspot)
        self.assertTrue(server.parse_args(["--no-hotspot"]).no_hotspot)
        self.assertFalse(server.hotspot_allowed(server.parse_args(["--monitor", "test"])))
        self.assertFalse(server.hotspot_allowed(server.parse_args(["--host", "127.0.0.1"])))


if __name__ == "__main__":
    unittest.main()
