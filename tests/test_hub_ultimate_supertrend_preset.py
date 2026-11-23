"""Preset "Ultimate Super Trend" (dashboard.html, HUB_PRESETS): the owner's rule choices AND numbers (only the timeframe is left). The preset
block is read out of the dashboard with node and the REAL plan check must accept it as it is.
Skipped when node is not installed."""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_hub_trading as T  # noqa: E402,F401  (stubs + temp data dir)
from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402

DASH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static", "dashboard.html")


def load_presets():
    src = open(DASH, encoding="utf-8").read()
    block = src.split("// PRESETS-BEGIN", 1)[1].split("// PRESETS-END", 1)[0]
    js = "function hubDeepFreeze(o){return o;}\n" + block + "\nconsole.log(JSON.stringify(HUB_PRESETS));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestUltimateSuperTrend(unittest.TestCase):
    def setUp(self):
        self.pr = next(p for p in load_presets() if p["id"] == "ultimate_supertrend")
        self.plan = self.pr["plan"]

    def test_name(self):
        self.assertEqual(self.pr["name"], "Ultimate Super Trend")

    def test_the_numbers(self):
        p = self.plan
        self.assertEqual((p["ind"]["sup"]["atr_length"], p["ind"]["sup"]["factor"]), (14, 5.25))
        self.assertEqual(p["entry_wait"], 0)
        sz = p["sizing"]
        self.assertEqual((sz["base_amount"], sz["leverage"], sz["stop_pct"]), (10000, 3, 15))
        self.assertNotIn("lock_pct", sz)                  # "% of last profit" stays empty
        self.assertNotIn("short_buffer", sz)              # empty = the default 2%
        self.assertEqual(p["shadow"], {"on": False})      # owner 2026-11-19: Shadow OFF by default
        for k in ("timeframe", "tf"):
            self.assertNotIn(k, p)                        # the timeframe is left for the owner

    def test_the_ticks(self):
        p = self.plan
        self.assertEqual(list(p["ind"]), ["sup"])
        s = p["ind"]["sup"]
        self.assertEqual((s["entry"], s["flip"], s["modes"]), (True, True, ["Trend direction"]))
        self.assertEqual((p["main_entry"], p["main_flip"], p["combine_entry"]), ("sup", "sup", "ALL"))
        self.assertTrue(p["wait_first_reversal"])
        self.assertFalse(p["allow_reentry"])
        self.assertEqual(p["flip_rules"], {"same_entry": True, "same_hold": True, "same_wait": True})
        sz = p["sizing"]
        self.assertTrue(sz["method"].startswith("Fixed base amount"))
        self.assertEqual(sz["stop_basis"], "% of entry amount")
        self.assertTrue(sz["stop_on"] and sz["short_enter"])
        self.assertFalse(sz["counter"]["on"])
        for k in ("hold", "sl", "trail", "be", "tp", "exit_when", "cooldown", "loss_limit", "shadow"):
            self.assertEqual(p[k], {"on": False}, k)

    def test_the_real_plan_check_accepts_it_as_it_is(self):
        pr = copy.deepcopy(self.plan)
        ind = pr.pop("ind")
        plan = make_plan(ind_sup=ind["sup"], **pr)
        H.validate_plan(plan)
        done = H.complete_plan(plan)
        text = " ".join(" ".join(map(str, r)) for r in H.plan_lines(done))
        self.assertIn("Supertrend", text)
        self.assertIn("enter with what is available", text)


if __name__ == "__main__":
    unittest.main()
