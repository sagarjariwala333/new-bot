"""Presets (2026-10-28): ready-made strategies in the Strategy Hub that hold ONLY rule choices, NO NUMBERS.
The preset data sits in dashboard.html between PRESETS-BEGIN and PRESETS-END; this test runs that block with Node.
Arbitrary test numbers only, NOT the owner's settings."""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._hub_plan import make_plan  # noqa: E402
from app import strategy_hub as H  # noqa: E402

DASHBOARD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static", "dashboard.html")


def load_presets():
    with open(DASHBOARD, encoding="utf-8") as fh:
        html = fh.read()
    m = re.search(r"// PRESETS-BEGIN\n(.*?)// PRESETS-END", html, re.S)
    assert m, "PRESETS-BEGIN / PRESETS-END markers not found"
    js = m.group(1) + "\nconsole.log(JSON.stringify(HUB_PRESETS));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def walk(o, path=()):
    """Every leaf of the plan with its path."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield from walk(v, path + (k,))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from walk(v, path + (i,))
    else:
        yield path, o


def merge(a, b):
    out = copy.deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@unittest.skipUnless(shutil.which("node"), "node is needed to read the preset block")
class TestPresets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.presets = load_presets()
        cls.by_name = {p["name"]: p for p in cls.presets}

    def test_the_presets_are_there(self):
        self.assertEqual([p["name"] for p in self.presets], ["HMA Flip", "MACD Cross", "V3", "RSI MA Cross", "EMA Cross", "MA 1 over MA 2"])
        self.assertEqual(len({p["id"] for p in self.presets}), 6)

    def test_no_numbers_in_any_plan(self):
        for p in self.presets:
            for path, v in walk(p["plan"]):
                self.assertNotIsInstance(v, (int, float)) if not isinstance(v, bool) else None
                if isinstance(v, str):
                    self.assertIsNone(re.fullmatch(r"-?\d+(\.\d+)?", v.strip()), (p["name"], path, v))
            # no key of a number box either
            bad = {"length", "value", "fast", "slow", "signal", "base_amount", "leverage", "lock_pct", "atr_length",
                   "atr_value", "alloc_pct", "stop_pct", "stop_usd", "adx_level", "entry_wait", "levels", "pct", "gap",
                   "alma_offset", "alma_sigma", "n", "di_length", "smoothing", "level", "ema_length", "tp_n"}
            keys = {str(k) for path, _ in walk(p["plan"]) for k in path}
            self.assertFalse(keys & bad, (p["name"], keys & bad))

    def test_no_digits_in_the_text_lines_except_the_names_the_hub_uses(self):
        for p in self.presets:
            for line in p["lines"] + [p["about"], p["name"]]:
                text = re.sub(r"MA [12]", "MA", line).replace("below 0", "below").replace("V3", "V")   # "V3" is the strategy's name   # "MA 1" / "MA 2" / the Hub's own label "Histogram above / below 0"
                self.assertIsNone(re.search(r"\d", text), (p["name"], line))

    def test_only_booleans_and_known_option_texts(self):
        ok_str = {"Candles", "HMA", "EMA", "MA rising / falling", H.MACD_HIST, H.SIZE_FIXED, H.BASIS_ATR,
                  H.TP_FROM_SIGNAL, H.TRAIL_CANDLE, "ma2",
                  "Heikin Ashi", "ALL", "Price above / below MA", H.SIZE_ALLOC, H.STOP_BAL, H.CT_EMA,   # V3 (2026-11-02)
                  "ma1", H.RSI_MODES[3],   # RSI MA Cross (2026-11-02)
                  H.MX_MODES[1],   # EMA Cross (2026-11-03)
                  H.MX_MODES[0]}   # MA 1 over MA 2 (2026-11-03)
        for p in self.presets:
            for path, v in walk(p["plan"]):
                self.assertTrue(isinstance(v, (bool, str)), (p["name"], path, v))
                if isinstance(v, str):
                    self.assertIn(v, ok_str, (p["name"], path))

    def test_hma_flip_choices(self):
        pl = self.by_name["HMA Flip"]["plan"]
        self.assertEqual(pl["chart_type"], "Candles")
        self.assertEqual(pl["ind"]["ma1"], {"entry": True, "type": "HMA", "modes": ["MA rising / falling"]})
        self.assertEqual(pl["ind"]["ma2"], {"type": "EMA"})                  # not ticked for ENTRY or FLIP
        self.assertNotIn("adx", pl["ind"])                                   # no ADX filter
        self.assertEqual(pl["flip_rules"], {"same_entry": True, "same_hold": True, "same_wait": True})
        self.assertIs(pl["hold"]["on"], True)
        self.assertIs(pl["wait_first_reversal"], True)
        self.assertIs(pl["allow_reentry"], False)                            # No Mid-Trend Re-entry ticked
        self.assertEqual(pl["sizing"]["method"], H.SIZE_FIXED)
        self.assertEqual(pl["sizing"]["counter"], {"on": True, "by": "ma2"})
        self.assertIs(pl["sizing"]["stop_on"], True)
        self.assertEqual(pl["sl"], {"on": True})
        self.assertEqual(pl["be"], {"on": True, "after_atr": True})
        self.assertEqual(pl["trail"], {"on": True, "type": H.TRAIL_CANDLE})  # signal candle high / low +/- ATR (owner 2026-10-30)
        self.assertEqual(pl["tp"], {"on": True, "basis": H.BASIS_ATR, "from": H.TP_FROM_SIGNAL})
        self.assertEqual(pl["shadow"], {"on": False})
        self.assertNotIn("timeframe", pl)

    def test_macd_cross_choices(self):
        pl = self.by_name["MACD Cross"]["plan"]
        self.assertEqual(pl["ind"], {"macd": {"entry": True, "flip": True, "modes": [H.MACD_HIST]}})   # NOT "Zero line"
        self.assertEqual(pl["sizing"], {"method": H.SIZE_FIXED})
        self.assertEqual(pl["sl"], {"on": True})
        self.assertEqual(pl["trail"], {"on": True, "type": H.TRAIL_CANDLE})
        self.assertEqual(pl["tp"], {"on": False})
        # what the owner has not answered is not in the preset
        for k in ("chart_type", "hold", "be", "shadow", "allow_reentry", "wait_first_reversal", "flip_rules"):
            self.assertNotIn(k, pl)
        self.assertNotIn("counter", pl["sizing"])
        self.assertNotIn("stop_on", pl["sizing"])

    def test_v3_choices(self):
        """V3 (owner 2026-11-02): the Base V3 rules as rule choices. Worked out from app/strategy.py (the V3 rules)."""
        pl = self.by_name["V3"]["plan"]
        self.assertEqual(pl["chart_type"], "Heikin Ashi")
        self.assertEqual(pl["combine_entry"], "ALL")
        self.assertEqual(pl["ind"], {
            "candle": {"entry": True},                                                          # HA candle up / down
            "ma1": {"entry": True, "type": "HMA", "modes": ["MA rising / falling"]},            # HMA rising / falling
            "ma2": {"entry": True, "type": "EMA", "modes": ["Price above / below MA"]}})        # close vs the filter EMA
        self.assertNotIn("flip", {k for b in pl["ind"].values() for k in b if b.get(k) is True})   # flip = same as entry
        self.assertEqual(pl["flip_rules"], {"same_entry": True, "same_hold": True, "same_wait": True})
        self.assertEqual(pl["hold"], {"on": True})
        self.assertIs(pl["wait_first_reversal"], True)
        self.assertIs(pl["allow_reentry"], False)                    # "Follow the main No Mid-Trend Re-entry" (owner)
        self.assertEqual(pl["sizing"], {"method": H.SIZE_ALLOC, "stop_on": True, "stop_basis": H.STOP_BAL,
                                        "counter": {"on": True, "by": H.CT_EMA}})
        for k in ("sl", "be", "trail", "tp"):
            self.assertEqual(pl[k], {"on": False}, k)
        self.assertEqual(pl["shadow"], {"on": True})
        self.assertNotIn("timeframe", pl)
        for k in ("cooldown", "loss_limit"):
            self.assertNotIn(k, pl)                                   # V3 has neither

    def test_rsi_ma_cross_choices(self):
        """RSI MA Cross (owner 2026-11-02): RSI crosses its own MA + MA 1 + ADX, no RSI level, Fixed base amount, no numbers."""
        pl = self.by_name["RSI MA Cross"]["plan"]
        self.assertEqual(pl["chart_type"], "Candles")
        self.assertEqual(pl["combine_entry"], "ALL")
        self.assertEqual(pl["ind"], {
            "rsi": {"entry": True, "modes": ["RSI crosses its own MA"]},            # that condition ONLY: no level, no OB/OS, no divergence
            "ma1": {"entry": True, "type": "HMA", "modes": ["MA rising / falling"]},
            "adx": {"entry": True}})
        self.assertEqual(pl["ind"]["rsi"]["modes"], [H.RSI_MODES[3]])
        self.assertEqual(pl["flip_rules"], {"same_entry": True, "same_hold": True, "same_wait": True})
        self.assertEqual(pl["hold"], {"on": True})
        self.assertIs(pl["wait_first_reversal"], True)
        self.assertIs(pl["allow_reentry"], False)
        self.assertEqual(pl["sizing"], {"method": H.SIZE_FIXED, "stop_on": True, "counter": {"on": True, "by": "ma1"}})
        self.assertNotIn("lock_pct", pl["sizing"])                                  # the % of last profit is HIS number
        self.assertEqual(pl["sl"], {"on": True})
        self.assertEqual(pl["trail"], {"on": True})                                  # type left for him
        for k in ("be", "tp"):
            self.assertEqual(pl[k], {"on": False}, k)
        self.assertEqual(pl["shadow"], {"on": True})
        self.assertEqual(pl["cooldown"], {"on": True})
        self.assertNotIn("loss_limit", pl)
        self.assertNotIn("timeframe", pl)

    def test_ema_cross_choices(self):
        """EMA Cross (owner 2026-11-03): MA Crossover only (EMA / EMA), flip all Yes, no Hold rule, Fixed base amount,
        Trailing Stop 'Signal candle high / low +/- ATR'; ADX and RSI there but NOT ticked; no numbers."""
        pl = self.by_name["EMA Cross"]["plan"]
        self.assertEqual(pl["chart_type"], "Candles")
        self.assertEqual(pl["ind"], {
            "mx": {"entry": True, "modes": [H.MX_MODES[1]]},
            "ma1": {"type": "EMA"}, "ma2": {"type": "EMA"}})                       # MA 1 / MA 2 only give the lines for the cross
        for blk in ("adx", "rsi"):
            self.assertNotIn(blk, pl["ind"])                                        # present in the form, not ticked
        self.assertEqual(pl["flip_rules"], {"same_entry": True, "same_hold": True, "same_wait": True})
        self.assertEqual(pl["hold"], {"on": False})
        self.assertEqual(pl["sizing"], {"method": H.SIZE_FIXED})
        self.assertNotIn("lock_pct", pl["sizing"])                                  # his number
        self.assertEqual(pl["trail"], {"on": True, "type": H.TRAIL_CANDLE})
        self.assertNotIn("value", pl["trail"])                                      # distance is his number
        for k in ("be", "tp"):
            self.assertEqual(pl[k], {"on": False}, k)
        for k in ("shadow", "cooldown", "loss_limit", "sl", "combine_entry", "timeframe"):
            self.assertNotIn(k, pl, k)                                              # left for the owner

    def test_ma_over_under_choices(self):
        """MA 1 over MA 2 (owner 2026-11-03): MA Crossover for ENTRY with 'MA 1 above / below MA 2' and NOTHING else."""
        pl = self.by_name["MA 1 over MA 2"]["plan"]
        self.assertEqual(pl, {"ind": {"mx": {"entry": True, "modes": [H.MX_MODES[0]]}}})
        self.assertEqual(H.MX_MODES[0], "MA 1 above / below MA 2")

    def test_option_texts_are_real_ones(self):
        self.assertIn("HMA", H.MA_TYPES)
        self.assertIn("MA rising / falling", H.MA_MODES)
        self.assertIn("Candles", H.CHART_TYPES)
        self.assertIn("ma2", H.CT_BLOCKS)
        self.assertIn(H.MACD_HIST, H.MACD_MODES)
        self.assertIn(H.TRAIL_CANDLE, H.TRAIL_TYPES)
        self.assertIn("Heikin Ashi", H.CHART_TYPES)
        self.assertIn("ALL", H.COMBINE)
        self.assertIn("Price above / below MA", H.MA_MODES)
        self.assertIn(H.SIZE_ALLOC, H.SIZE_METHODS)
        self.assertIn(H.STOP_BAL, H.STOP_BASES)
        self.assertIn(H.CT_EMA, H.CT_SOURCES)
        self.assertIn(H.RSI_MODES[3], H.RSI_MODES)
        self.assertEqual(H.MX_MODES[1], "MA 1 crossed MA 2 within N candles")
        self.assertEqual(H.RSI_MODES[3], "RSI crosses its own MA")


@unittest.skipUnless(shutil.which("node"), "node is needed to read the preset block")
class TestPresetPlusNumbersIsAValidPlan(unittest.TestCase):
    """A preset with the owner's own numbers keyed in (arbitrary ones here) is accepted by the plan check."""

    @classmethod
    def setUpClass(cls):
        cls.by_name = {p["name"]: p for p in load_presets()}

    def base(self):
        p = make_plan()
        for b in ("ma1", "ma2"):
            p["ind"][b].pop("mode", None)
        p["sizing"] = {}
        p["sl"] = {}
        p["trail"] = {}
        p["tp"] = {}
        p["hold"] = {}
        p.pop("entry_wait", None)
        return p

    def test_hma_flip_with_numbers(self):
        pre = self.by_name["HMA Flip"]["plan"]
        numbers = {
            "combine_entry": "ALL", "entry_wait": 0,
            "ind": {"ma1": {"length": 9, "n": 1}, "ma2": {"length": 40, "n": 1, "modes": ["Price above / below MA"]},   # MA 2 needs a condition ticked
                    "adx": {"di_length": 7, "smoothing": 7, "level": 15.0, "di_required": False}},
            "hold": {"adx_level": 20.0},
            "sizing": {"base_amount": 50.0, "leverage": 3, "lock_pct": 40.0,
                       "counter": {"base_amount": 20.0, "leverage": 2},
                       "stop_basis": H.STOP_ENTRY, "stop_pct": 5.0},
            "sl": {"basis": H.BASIS_ATR, "value": 150.0, "atr_length": 9},
            "be": {"atr_value": 100.0, "atr_length": 9},
            "trail": {"value": 100.0, "atr_length": 9},
            "tp": {"atr_length": 9, "levels": [{"on": True, "value": 100.0, "pct": 50.0},
                                               {"on": False}, {"on": False}]},
        }
        plan = merge(merge(self.base(), pre), numbers)
        plan["name"] = "HMA Flip"
        done = H.validate_plan(H.complete_plan(H.upgrade_plan(plan)))
        self.assertIsNotNone(done)

    def test_macd_cross_with_numbers(self):
        pre = self.by_name["MACD Cross"]["plan"]
        numbers = {
            "chart_type": "Candles", "combine_entry": "ALL", "entry_wait": 0,
            "hold": {"on": False},
            "ind": {"macd": {"fast": 5, "slow": 13, "signal": 4, "rising": False}},
            "sizing": {"base_amount": 50.0, "leverage": 3, "lock_pct": 40.0, "stop_on": False},
            "sl": {"basis": H.BASIS_PRICE, "value": 3.0},
            "trail": {"value": 100.0, "atr_length": 9},
            "be": {"on": False}, "shadow": {"on": False},
        }
        plan = merge(merge(self.base(), pre), numbers)
        plan["name"] = "MACD Cross"
        done = H.validate_plan(H.complete_plan(H.upgrade_plan(plan)))
        self.assertIsNotNone(done)


class TestV3PlusNumbersIsAValidPlan(TestPresetPlusNumbersIsAValidPlan):
    """V3 with the owner's own numbers keyed in (arbitrary ones here) is accepted by the plan check."""

    def test_v3_with_numbers(self):
        pre = self.by_name["V3"]["plan"]
        numbers = {
            "entry_wait": 0,
            "ind": {"ma1": {"length": 9}, "ma2": {"length": 40},
                    "adx": {"di_length": 7, "smoothing": 7, "level": 15.0, "di_required": False}},
            "hold": {"adx_level": 20.0},
            "sizing": {"alloc_pct": 20.0, "leverage": 5,
                       "counter": {"alloc_pct": 10.0, "leverage": 3, "ema_length": 100},
                       "stop_pct": 6.0},
            "shadow": {"trades": 3, "slippage_ticks": 1, "tracker_start": 100.0},
        }
        plan = merge(merge(self.base(), pre), numbers)
        plan["name"] = "V3"
        done = H.validate_plan(H.complete_plan(H.upgrade_plan(plan)))
        self.assertIsNotNone(done)
        # without the owner's numbers it is NOT a plan yet (nothing was filled in for him)
        with self.assertRaises(H.PlanError):
            H.validate_plan(merge(self.base(), pre))

    def test_no_numbers_no_trade_each_missing_number_is_refused(self):
        pre = self.by_name["V3"]["plan"]
        full = merge(merge(self.base(), pre), {
            "entry_wait": 0, "ind": {"ma1": {"length": 9}, "ma2": {"length": 40},
                                     "adx": {"di_length": 7, "smoothing": 7, "level": 15.0, "di_required": False}},
            "hold": {"adx_level": 20.0},
            "sizing": {"alloc_pct": 20.0, "leverage": 5, "counter": {"alloc_pct": 10.0, "leverage": 3, "ema_length": 100},
                       "stop_pct": 6.0}, "shadow": {"trades": 3, "slippage_ticks": 1, "tracker_start": 100.0}})
        full["name"] = "V3"
        H.validate_plan(full)
        for path in (("ind", "ma1", "length"), ("ind", "ma2", "length"), ("sizing", "alloc_pct"), ("sizing", "leverage"),
                     ("sizing", "stop_pct"), ("hold", "adx_level"), ("shadow", "trades"),
                     ("shadow", "slippage_ticks"), ("shadow", "tracker_start")):
            bad = copy.deepcopy(full)
            d = bad
            for k in path[:-1]:
                d = d[k]
            d.pop(path[-1])
            with self.subTest(missing=path), self.assertRaises(H.PlanError):
                H.validate_plan(bad)


class TestRsiMaCrossPlusNumbersIsAValidPlan(TestPresetPlusNumbersIsAValidPlan):
    """RSI MA Cross with the owner's own numbers keyed in (arbitrary ones here) is accepted by the plan check."""

    def full(self):
        pre = self.by_name["RSI MA Cross"]["plan"]
        return merge(merge(self.base(), pre), {
            "entry_wait": 0,
            "ind": {"rsi": {"length": 14, "ma_type": "SMA", "ma_length": 9, "n": 3},
                    "ma1": {"length": 9},
                    "adx": {"di_length": 7, "smoothing": 7, "level": 15.0, "di_required": False, "rising": False}},
            "hold": {"adx_level": 20.0},
            "sizing": {"base_amount": 50.0, "leverage": 5, "lock_pct": 40.0, "stop_basis": H.STOP_BAL, "stop_pct": 6.0,
                       "counter": {"base_amount": 20.0, "leverage": 3}},
            "sl": {"basis": "% of price movement", "value": 3.0},
            "trail": {"type": "Signal candle high / low +/- ATR", "atr_length": 9, "value": 100.0},
            "shadow": {"trades": 3, "slippage_ticks": 1, "tracker_start": 100.0},
            "cooldown": {"candles": 4}})

    def test_rsi_ma_cross_with_numbers(self):
        plan = self.full()
        plan["name"] = "RSI MA Cross"
        self.assertIsNotNone(H.validate_plan(H.complete_plan(H.upgrade_plan(plan))))
        with self.assertRaises(H.PlanError):                       # without his numbers it is NOT a plan yet
            H.validate_plan(merge(self.base(), self.by_name["RSI MA Cross"]["plan"]))

    def test_each_missing_number_is_refused(self):
        full = self.full(); full["name"] = "RSI MA Cross"
        H.validate_plan(full)
        for path in (("ind", "rsi", "length"), ("ind", "rsi", "ma_length"), ("ind", "rsi", "n"), ("ind", "ma1", "length"),
                     ("ind", "adx", "level"), ("sizing", "base_amount"), ("sizing", "leverage"), ("sizing", "stop_pct"),
                     ("hold", "adx_level"), ("shadow", "trades"), ("cooldown", "candles")):
            bad = copy.deepcopy(full)
            d = bad
            for k in path[:-1]:
                d = d[k]
            d.pop(path[-1])
            with self.subTest(missing=path), self.assertRaises(H.PlanError):
                H.validate_plan(bad)


class TestEmaCrossPlusNumbersIsAValidPlan(TestPresetPlusNumbersIsAValidPlan):
    """EMA Cross with the owner's own numbers keyed in (arbitrary ones here) is accepted by the plan check."""

    def full(self):
        pre = self.by_name["EMA Cross"]["plan"]
        return merge(merge(self.base(), pre), {
            "entry_wait": 0,
            # the plan check asks for ONE condition ticked on MA 1 and on MA 2 (not for ENTRY: it does not change the signal)
            "ind": {"mx": {"n": 0}, "ma1": {"length": 9, "modes": ["MA rising / falling"]},
                    "ma2": {"length": 21, "modes": ["MA rising / falling"]}},
            "sizing": {"base_amount": 50.0, "leverage": 5, "lock_pct": 50.0, "stop_on": False},   # Max Loss: his choice (off here)
            "sl": {"on": False},                                     # Stop Loss: his choice (off here)
            "trail": {"atr_length": 14, "value": 0.0001}})

    def test_ema_cross_with_numbers(self):
        plan = self.full()
        plan["name"] = "EMA Cross"
        self.assertIsNotNone(H.validate_plan(H.complete_plan(H.upgrade_plan(plan))))
        with self.assertRaises(H.PlanError):                       # without his numbers it is NOT a plan yet
            H.validate_plan(merge(self.base(), self.by_name["EMA Cross"]["plan"]))

    def test_distance_zero_is_refused_smallest_is_accepted(self):
        full = self.full(); full["name"] = "EMA Cross"
        H.validate_plan(full)                                       # 0.0001 (practically 0) is accepted
        bad = copy.deepcopy(full); bad["trail"]["value"] = 0
        with self.assertRaises(H.PlanError):
            H.validate_plan(bad)

    def test_each_missing_number_is_refused(self):
        full = self.full(); full["name"] = "EMA Cross"
        H.validate_plan(full)
        for path in (("ind", "mx", "n"), ("ind", "ma1", "length"), ("ind", "ma2", "length"), ("sizing", "base_amount"),
                     ("sizing", "leverage"), ("trail", "atr_length"), ("trail", "value")):
            bad = copy.deepcopy(full)
            d = bad
            for k in path[:-1]:
                d = d[k]
            d.pop(path[-1])
            with self.subTest(missing=path), self.assertRaises(H.PlanError):
                H.validate_plan(bad)

    def test_the_condition_ticked_on_ma1_ma2_does_not_change_the_signal(self):
        """The plan check wants a condition on MA 1 and MA 2, but only the cross gives the entry (their ENTRY is not ticked)."""
        import numpy as np, pandas as pd
        rng = np.random.default_rng(3); n = 1500
        close = 100 + np.cumsum(rng.normal(0, 0.6, n))
        op = np.r_[close[0], close[:-1]]
        df = pd.DataFrame({"open_time": np.arange(n) * 3600000, "open": op, "close": close,
                           "high": np.maximum(op, close) + rng.random(n) * 0.4, "low": np.minimum(op, close) - rng.random(n) * 0.4,
                           "volume": rng.random(n) * 100 + 10})
        a = self.full(); a["name"] = "EMA Cross"
        b = copy.deepcopy(a); b["ind"]["ma1"]["modes"] = ["Price above / below MA"]; b["ind"]["ma2"]["modes"] = ["MA rising / falling", "Price above / below MA"]
        oa, ob = H.compute_hub(df, a), H.compute_hub(df, b)
        for col in ("entry_long", "entry_short", "flip_long", "flip_short"):
            self.assertTrue((oa[col].astype(bool) == ob[col].astype(bool)).all(), col)
        self.assertTrue(oa["entry_long"].astype(bool).any() and oa["entry_short"].astype(bool).any())    # the cross does fire
        e, f = oa["ma1"].values, oa["ma2"].values                                     # N = 0: only on the candle of the cross
        up = np.r_[False, (e[1:] > f[1:]) & (e[:-1] <= f[:-1])]
        self.assertTrue((oa["entry_long"].astype(bool).values == up).all())

    def test_adx_and_rsi_can_be_ticked_on_top(self):
        """He may tick ADX / RSI after Load: the plan is still valid once their numbers are keyed in."""
        full = self.full(); full["name"] = "EMA Cross"
        full["ind"]["adx"] = {"entry": True, "flip": False, "di_length": 7, "smoothing": 7, "level": 15.0, "di_required": False, "rising": False}
        full["ind"]["rsi"] = {"entry": True, "flip": False, "modes": ["Above / below level"], "length": 14, "level": 50.0}
        full["combine_entry"] = "ALL"
        self.assertIsNotNone(H.validate_plan(H.complete_plan(H.upgrade_plan(full))))


class TestMaOverUnderPlusNumbersIsAValidPlan(TestPresetPlusNumbersIsAValidPlan):
    """MA 1 over MA 2 with the owner's own choices and numbers keyed in (arbitrary ones here) is a valid plan."""

    def full(self):
        pre = self.by_name["MA 1 over MA 2"]["plan"]
        return merge(merge(self.base(), pre), {
            "chart_type": "Candles", "combine_entry": "ALL", "entry_wait": 0,
            "ind": {"ma1": {"type": "EMA", "length": 9, "modes": ["MA rising / falling"]},
                    "ma2": {"type": "SMA", "length": 21, "modes": ["MA rising / falling"]}},
            "hold": {"on": False},
            "sizing": {"method": H.SIZE_FIXED, "base_amount": 50.0, "leverage": 5, "stop_on": False},
            "sl": {"on": False}, "trail": {"on": False}, "tp": {"on": False}, "be": {"on": False}})

    def test_ma_over_under_with_choices_and_numbers(self):
        plan = self.full(); plan["name"] = "MA 1 over MA 2"
        self.assertIsNotNone(H.validate_plan(H.complete_plan(H.upgrade_plan(plan))))
        with self.assertRaises(H.PlanError):
            H.validate_plan(merge(self.base(), self.by_name["MA 1 over MA 2"]["plan"]))   # not a plan before he fills it

    def test_each_missing_number_is_refused(self):
        full = self.full(); full["name"] = "MA 1 over MA 2"
        H.validate_plan(full)
        for path in (("ind", "ma1", "length"), ("ind", "ma2", "length"), ("ind", "ma1", "type"), ("ind", "ma2", "type"),
                     ("sizing", "base_amount"), ("sizing", "leverage")):
            bad = copy.deepcopy(full)
            d = bad
            for k in path[:-1]:
                d = d[k]
            d.pop(path[-1])
            with self.subTest(missing=path), self.assertRaises(H.PlanError):
                H.validate_plan(bad)

    def test_signal_is_ma1_above_or_below_ma2(self):
        """Long while MA 1 is above MA 2, short while below (checked against a plain pandas EWM / rolling oracle)."""
        import numpy as np, pandas as pd
        rng = np.random.default_rng(5); n = 800
        close = 100 + np.cumsum(rng.normal(0, 0.6, n))
        op = np.r_[close[0], close[:-1]]
        df = pd.DataFrame({"open_time": np.arange(n) * 3600000, "open": op, "close": close,
                           "high": np.maximum(op, close) + 0.3, "low": np.minimum(op, close) - 0.3, "volume": np.full(n, 50.0)})
        plan = self.full(); plan["name"] = "MA 1 over MA 2"
        out = H.compute_hub(df, plan)
        e = pd.Series(close).ewm(span=9, adjust=False).mean().values
        f = pd.Series(close).rolling(21).mean().values
        ok = ~np.isnan(f)
        lg, sh = out["entry_long"].astype(bool).values, out["entry_short"].astype(bool).values
        warm = 60                                                         # past the EMA start-up
        self.assertTrue((lg[warm:] == (e > f)[warm:]).all())
        self.assertTrue((sh[warm:] == (e < f)[warm:]).all())
        self.assertTrue(lg[warm:].any() and sh[warm:].any())


if __name__ == "__main__":
    unittest.main()
