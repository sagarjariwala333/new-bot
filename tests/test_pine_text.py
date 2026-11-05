"""Text checks of pine/STRATEGY_HUB_BLANK.pine (cannot be compiled here - TradingView needed)."""
import re, unittest
from pathlib import Path

PINE = (Path(__file__).resolve().parent.parent / "pine" / "STRATEGY_HUB_BLANK.pine").read_text()
LINES = PINE.splitlines()
NEW_IDS = ["cdPrev", "bbE", "bbF", "bbC", "bbLen", "bbMult", "bbN", "suE", "suF", "suTrendOn", "suFlipOn",
           "suAtrLen", "suFactor", "suN", "exOn", "exSignal", "exAfter", "exLong", "exShort", "reForced",
           "exWait", "suLineV", "bbBasis"]


def first_use(name):
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
    for i, ln in enumerate(LINES):
        code = ln.split("//")[0]
        if pat.search(code):
            return i, code
    return None, ""


class PineText(unittest.TestCase):
    def test_new_identifiers_defined_before_use(self):
        for n in NEW_IDS:
            i, code = first_use(n)
            self.assertIsNotNone(i, n + " missing")
            pat = r"(^|[\s,(])(var\s+|float\s+|int\s+|bool\s+|string\s+)?" + re.escape(n) + r"\s*(:=|=)"
            self.assertTrue(re.search(pat, code), f"{n} first appears as a use, not a definition (line {i+1}): {code.strip()}")

    def test_trail_start_and_swing_stop_options_are_in_the_script(self):
        # owner 2026-11-05 ("Both"): Signal candle trail may start "Only once in profit"; Stop Loss Structure by swing point
        for n in ("trCStart", "trCProfit", "tcArm", "slStruct", "slPivN", "slPivLow", "slPivHigh"):
            i, code = first_use(n)
            self.assertIsNotNone(i, n + " missing")
            pat = r"(^|[\s,(])(var\s+|float\s+|int\s+|bool\s+|string\s+)?(array<float>\s+)?" + re.escape(n) + r"\s*(:=|=)"
            self.assertTrue(re.search(pat, code), f"{n} first appears as a use (line {i+1}): {code.strip()}")
        self.assertIn('"Only once in profit"', PINE)
        self.assertIn('"Real swing point (N candles each side)"', PINE)
        self.assertIn("ta.pivotlow(low, slPivN, slPivN)", PINE)
        self.assertIn("ta.pivothigh(high, slPivN, slPivN)", PINE)
        self.assertIn("tcArm := na", PINE)                                  # cleared at every entry

    def test_cnt_has_15_arguments_everywhere(self):
        self.assertIn("cnt(bool t1, bool t2, bool t3, bool t4, bool t5, bool t6, bool t7, bool t8, bool t9, bool t10, bool t11, bool t12, bool t13, bool t14, bool t15)", PINE)
        calls = re.findall(r"^n[EF][LS]?\s*=\s*cnt\((.*)\)\s*$", PINE, flags=re.M)
        self.assertEqual(len(calls), 6)
        for c in calls:
            depth, parts, cur = 0, [], ""
            for ch in c:
                if ch == "(": depth += 1
                if ch == ")": depth -= 1
                if ch == "," and depth == 0: parts.append(cur); cur = ""
                else: cur += ch
            parts.append(cur)
            self.assertEqual(len(parts), 15, c)

    def test_ema_band_and_parabolic_sar_are_in_the_script(self):
        # owner 2026-11-05: EMA Band (3 EMAs on high / close / low) and Parabolic SAR as blocks, with ENTRY / FLIP / CTREND
        for n in ("ebE", "ebF", "ebC", "ebLen", "ebN", "ebLL", "ebLH", "ebSH", "ebSL", "ebL", "ebS", "saE", "saF", "saC",
                  "saStep", "saMax", "saL", "saS", "saV"):
            i, code = first_use(n)
            self.assertIsNotNone(i, n + " missing")
            pat = r"(^|[\s,(])(var\s+|float\s+|int\s+|bool\s+|string\s+)?" + re.escape(n) + r"\s*(:=|=)"
            self.assertTrue(re.search(pat, code), f"{n} first appears as a use (line {i+1}): {code.strip()}")
        self.assertIn("ebTop = ta.ema(cH, ebLen)", PINE)
        self.assertIn("ebBot = ta.ema(cL, ebLen)", PINE)
        self.assertIn('"EMA Band" => ebL', PINE)
        self.assertIn('"Parabolic SAR" => saS', PINE)
        self.assertIn('ebC ? "EMA Band" : saC ? "Parabolic SAR"', PINE)

    def test_options_present(self):
        for s in ["Supertrend line", "Both MA 1 and MA 2 curve against the trade", "MA 2 only (the slower one) curves against the trade",
                  "Close and wait for a fresh signal", "Close and flip to the other side"]:
            self.assertIn(s, PINE)

    def test_no_barssince_or_ta_call_sits_on_the_right_of_an_and_or_or(self):
        # Pine v6 skips the right side of and / or when the left side decides it, so a ta.* call or within() (ta.barssince)
        # there is not run on every candle and gives wrong history. Work it out on its own line first.
        for i, ln in enumerate(LINES):
            code = ln.split("//")[0]
            self.assertIsNone(re.search(r"\b(and|or)\s+\(?\s*(within\(|ta\.)", code), f"line {i+1}: {code.strip()}")

    def test_ema_band_was_other_side_checks_run_on_their_own_lines(self):
        for n in ("ebWasBelowBot", "ebWasBelowTop", "ebWasAboveTop", "ebWasAboveBot"):
            i, code = first_use(n)
            self.assertIsNotNone(i, n + " missing")
            self.assertRegex(code, r"^\s*" + n + r"\s*=\s*within\(", f"{n} must be defined by within() on its own line")
        for n in ("ebUpBot", "ebUpTop", "ebDnTop", "ebDnBot"):
            i, code = first_use(n)
            self.assertNotIn("within(", code, n)

    def test_balanced_brackets(self):
        code = "\n".join(l.split("//")[0] for l in LINES)
        code = re.sub(r'"[^"\n]*"', '""', code)
        for a, b in ("()", "[]"):
            self.assertEqual(code.count(a), code.count(b), a + b)

    def test_header_says_15_tabs(self):
        self.assertIn("15", "\n".join(LINES[:15]))


if __name__ == "__main__":
    unittest.main()
