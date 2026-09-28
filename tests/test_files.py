"""File names for photos and files sent from the iPad."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import safe_filename, unique_path  # noqa: E402


class SafeFilenameTests(unittest.TestCase):
    def test_keeps_normal_names(self):
        self.assertEqual(safe_filename("IMG_1234.HEIC"), "IMG_1234.HEIC")
        self.assertEqual(safe_filename("사진 1.jpg"), "사진 1.jpg")

    def test_strips_folders_so_files_stay_in_the_save_folder(self):
        self.assertEqual(safe_filename("../../Windows/system.ini"), "system.ini")
        self.assertEqual(safe_filename("C:\\Users\\x\\evil.bat"), "evil.bat")
        self.assertEqual(safe_filename("..."), "file")

    def test_replaces_characters_windows_forbids(self):
        self.assertEqual(safe_filename('a<b>c:d"e|f?g*h.png'), "a_b_c_d_e_f_g_h.png")
        self.assertEqual(safe_filename("tab\there.txt"), "tab_here.txt")

    def test_reserved_device_names(self):
        self.assertEqual(safe_filename("CON.txt"), "file_CON.txt")
        self.assertEqual(safe_filename("lpt1"), "file_lpt1")
        self.assertEqual(safe_filename(""), "file")

    def test_long_names_keep_their_extension(self):
        name = safe_filename("x" * 300 + ".jpeg")
        self.assertEqual(len(name), 150)
        self.assertTrue(name.endswith(".jpeg"))

    def test_unique_path_numbers_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            first = Path(d) / "photo.jpg"
            self.assertEqual(unique_path(first), first)
            first.write_bytes(b"1")
            second = unique_path(first)
            self.assertEqual(second.name, "photo (2).jpg")
            second.write_bytes(b"2")
            self.assertEqual(unique_path(first).name, "photo (3).jpg")


if __name__ == "__main__":
    unittest.main()
