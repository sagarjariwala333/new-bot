"""
_hub_plan.py  -  TEST-ONLY helper: a complete, valid Strategy Hub plan with every indicator OFF
except what a test switches on. Arbitrary numbers, NOT the owner's settings. The product has no default plan.
"""
import copy

_MA = dict(entry=False, flip=False, type="EMA", length=10, mode="Price above / below MA", n=1,
           alma_offset=0.85, alma_sigma=6.0)


def base_plan() -> dict:
    return {
        "chart_type": "Candles", "renko_atr_length": 14,
        "combine_entry": "ALL", "combine_flip": "ALL",
        "flip_action": "Reverse (close + open opposite)", "entry_wait": 0, "wait_first_reversal": False,
        "ind": {
            "candle": dict(entry=False, flip=False),
            "ma1": dict(_MA), "ma2": dict(_MA, length=30),
            "mx": dict(entry=False, flip=False, mode="MA 1 above / below MA 2", n=1),
            "adx": dict(entry=False, flip=False, di_length=14, smoothing=14, level=20.0, di_required=True),
            "ci": dict(entry=False, flip=False, length=14, max=55.0),
            "rsi": dict(entry=False, flip=False, length=14, level=50.0),
            "macd": dict(entry=False, flip=False, fast=12, slow=26, signal=9, rising=False),
            "vol": dict(entry=False, flip=False, length=20, mult=1.0),
            "atr": dict(entry=False, flip=False, length=14, avg_length=20, mult=1.0),
            "st": dict(entry=False, flip=False, length=10),
        },
        "hold": {"on": False, "adx_level": 25.0},
        "sizing": {"alloc_pct": 10.0, "leverage": 5, "stop_on": True, "stop_pct": 6.0},
        "tp": {"on": False, "atr_length": 14,
               "levels": [{"on": True, "mult": 1.0, "pct": 33.0}, {"on": False, "mult": 3.0, "pct": 33.0},
                          {"on": False, "mult": 5.0, "pct": 34.0}]},
        "trail": {"on": False, "length": 10},
        # Stop Loss is a must (2026-10-26). A FAR one (99.9% of the price) so the tests' Max Loss stays in charge.
        "sl": {"on": True, "basis": "% of price movement", "value": 99.9, "atr_length": 14},
    }


def make_plan(**over) -> dict:
    """make_plan(ma1={'entry': True, ...}, chart_type='Heikin Ashi', ...) - dict values are merged into
    the matching section; ind blocks are given under ind_<block>=..."""
    p = base_plan()
    for k, v in over.items():
        if k.startswith("ind_"):
            p["ind"][k[4:]].update(v)
        elif isinstance(v, dict) and isinstance(p.get(k), dict):
            p[k].update(v)
        else:
            p[k] = v
    cb = p["ind"]["candle"]
    if (cb["entry"] or cb["flip"]) and "chart_type" not in over:
        p["chart_type"] = "Heikin Ashi"      # Candle colour is allowed only on Heikin Ashi / Renko
    return copy.deepcopy(p)
