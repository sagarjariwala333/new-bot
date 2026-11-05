"""The owner's own look of the Strategy Hub menu (2026-11-05): names, sub-texts and order, saved on the server."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import hub_menu_layout as M  # noqa: E402


class TestMenuLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = (M.DATA_DIR, M.LAYOUT_FILE)
        M.DATA_DIR = Path(self.tmp.name)
        M.LAYOUT_FILE = M.DATA_DIR / "hub_menu_layout.json"

    def tearDown(self):
        M.DATA_DIR, M.LAYOUT_FILE = self._old
        self.tmp.cleanup()

    def test_nothing_saved_means_the_original_menu(self):
        self.assertEqual(M.load(), {"order": [], "names": {}, "descs": {}})

    def test_save_and_load_round_trip(self):
        lay = {"order": ["pair", "size", "ind"], "names": {"size": "Sizing"}, "descs": {"pair": "My pair"}}
        self.assertEqual(M.save(lay), lay)
        self.assertEqual(M.load(), lay)
        self.assertTrue(M.LAYOUT_FILE.exists())

    def test_texts_are_trimmed_and_empty_texts_are_dropped(self):
        out = M.save({"order": ["pair"], "names": {"pair": "  My   pair  ", "size": "   "}, "descs": {"pair": ""}})
        self.assertEqual(out["names"], {"pair": "My pair"})
        self.assertEqual(out["descs"], {})

    def test_bad_input_is_refused_and_nothing_is_written(self):
        bad = [
            "x", {"order": "pair"}, {"order": ["pair", "pair"]}, {"order": ["Bad Id"]}, {"order": [1]},
            {"names": {"pair": "x" * (M.MAX_NAME + 1)}}, {"descs": {"pair": "x" * (M.MAX_DESC + 1)}},
            {"names": {"pair": 5}}, {"names": {"../x": "a"}}, {"order": ["a"] * 0 + [f"f{i}" for i in range(M.MAX_FUNCS + 1)]},
        ]
        for b in bad:
            with self.assertRaises(M.MenuLayoutError, msg=str(b)[:60]):
                M.save(b)
        self.assertFalse(M.LAYOUT_FILE.exists())

    def test_a_broken_file_gives_the_original_menu(self):
        M.LAYOUT_FILE.write_text("{not json")
        self.assertEqual(M.load(), M.blank())
        M.LAYOUT_FILE.write_text(json.dumps({"order": ["Bad Id"]}))
        self.assertEqual(M.load(), M.blank())


if __name__ == "__main__":
    unittest.main()
