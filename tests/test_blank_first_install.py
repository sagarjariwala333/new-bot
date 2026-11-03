"""A first install has NO pre-filled values: every field is blank, nothing is confirmed, live switch OFF."""
import shutil, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app.default_settings as ds  # noqa: E402


class TestBlankFirstInstall(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="blank_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_no_starter_values_exist(self):
        self.assertFalse(hasattr(ds, "STARTER_VALUES"))

    def test_first_install_is_all_blank_and_unconfirmed(self):
        s = ds.DefaultSettingsStore(path=self.dir / "default_settings.json")
        for plat in ds.PLATFORMS:
            g = s.get(plat)
            self.assertFalse(g["confirmed"])
            self.assertTrue(all(v is None for v in g["values"].values()), plat)
            self.assertEqual(sorted(g["missing"]), sorted(ds.fields_for(plat)))
            with self.assertRaises(ds.DefaultsNotConfirmed):
                s.confirmed_values(plat)
        self.assertFalse(s.live_trading_enabled())

    def test_the_real_store_is_blank_too(self):
        for plat in ds.PLATFORMS:
            self.assertEqual(ds.DefaultSettingsStore(path=self.dir / "x.json").get(plat)["values"]
                             , {n: None for n in ds.fields_for(plat)})


if __name__ == "__main__":
    unittest.main()
