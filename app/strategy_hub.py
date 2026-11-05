"""
strategy_hub.py
===============

STRATEGY HUB - pure calculation (no exchange calls, no I/O). Mirrors the TradingView test script
"STRATEGY HUB [TEST]" line by line.

There is NO default strategy. A "plan" is a dict the owner fills in on the dashboard:
pair, timeframe, chart type, the indicators ticked for ENTRY, the indicators ticked for FLIP,
entry wait, sizing / max loss, take profit, trailing stop. Every value is required (validate_plan
refuses a plan with anything missing); nothing in this file supplies a default.

What this module does
  * builds the chart-type candles (Candles / Heikin Ashi / Renko) from the normal candles,
  * computes every indicator the plan needs,
  * gives every indicator block a LONG and a SHORT condition,
  * combines the ticked ENTRY blocks and the ticked FLIP blocks (ALL or ANY) into
      dir_e  (entry signal : 1 long / -1 short / 0 none)
      dir_f  (flip signal  : 1 long / -1 short / 0 none),
  * gives the take-profit ATR and the trailing-stop extremes.

What it does NOT do: place orders, size trades, hold rule / flip order / entry wait (those are in
app/instance.py and the small helper functions at the bottom of this file).

Renko note: TradingView builds Renko from its own tick data. Here Renko is built from the CLOSED
candles (close based, box size = ATR). It follows the same rules but is NOT guaranteed to produce
exactly the same bricks as TradingView.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from app import sr_engine as _sr
from app import strategy as _strat
from app.strategy import (  # noqa: F401  (shared, Pine-exact helpers)
    pine_sma, pine_ema, pine_wma, pine_hma, pine_rma, pine_dmi, _shift, _to_np,
)

CHART_TYPES = ("Candles", "Heikin Ashi", "Renko")
MA_TYPES = ("SMA", "EMA", "WMA", "HMA", "ALMA")
MA_PULLBACK = "Pullback to MA (bounce)"   # 2026-10-25
MA_MODES = ("Price above / below MA", "MA rising / falling", "Price crossed MA within N candles", MA_PULLBACK)
MX_MODES = ("MA 1 above / below MA 2", "MA 1 crossed MA 2 within N candles")
COMBINE_ALL = "ALL"
COMBINE_ANY = "ANY"
COMBINE_ATLEAST = "At least N"
# ALL = every ticked indicator must agree, ANY = one is enough, At least N = the owner keys in how many must agree
# (entry_min / flip_min). A plan with ALL or ANY has no N.
COMBINE = (COMBINE_ALL, COMBINE_ANY, COMBINE_ATLEAST)
# A FLIP (owner 2026-10-25): "A flip is closing current position. Checks for any changes while in flat. Adopt the
# change if needed and enter based on the new signal" - the flip signal IS the new entry signal, so a flip always
# opens the new side. "Close only" was removed ("It's complicating things"); an older plan with it becomes a flip.
FLIP_ACTIONS = ("Reverse (close + open opposite)",)
FLIP_CLOSE_ONLY_OLD = "Close only (go flat)"
# FLIP TAB (owner 2026-10-25): three Yes / No questions. Yes = the flip follows the main settings.
#   same_entry: Yes = the flip uses the ENTRY signal (ENTRY ticks, Main, Indicator Combination);
#               No  = its own FLIP ticks, Main for FLIP, Indicator Combination for FLIP
#   same_hold : Yes = the Hold rule tab;  No = the flip's own hold (hold_on + hold_level)
#   same_wait : Yes = the Entry wait;     No = the flip's own wait (wait candles, 0 = instant)
# New plans start with all Yes (dashboard). A plan saved before 2026-10-25 keeps what it did: its FLIP ticks
# (same_entry No), the Hold rule tab and the Entry wait (upgrade_plan).

# ENTRY SIZE method. A plan with no "method" uses the allocation % (the way plans were always saved).
SIZE_ALLOC = "Allocation % of balance"
SIZE_FIXED = "Fixed base amount (grows with the last closed total)"
SIZE_METHODS = (SIZE_ALLOC, SIZE_FIXED)
# MAX LOSS basis. A plan with no "stop_basis" uses % of the available balance (the way plans were always saved).
STOP_BAL = "% of available balance"
STOP_ENTRY = "% of entry amount"
STOP_USD = "Fixed $ amount"
STOP_BASES = (STOP_BAL, STOP_ENTRY, STOP_USD)

# DISTANCE BASIS of Stop Loss, Trailing Stop (Step) and Take Profit (owner rule 2026-10-23, one standard):
#   "% of ATR"            : 100 = 1 ATR, 200 = 2 ATR, 50 = 0.5 ATR. ATR of the NORMAL candles at the entry candle,
#                           fixed for the whole trade.
#   "% of price movement" : 3 = 3% of the entry price, 0.5 = 0.5%. The price move only - leverage plays no part.
BASIS_ATR = "% of ATR"
BASIS_PRICE = "% of price movement"
BASES = (BASIS_ATR, BASIS_PRICE)
# TAKE PROFIT "Measured from" (owner 2026-10-27, "ADD this option, for future use"): the price the 3 targets are
# counted from. "Entry price" = the real fill (as before, the default; a plan without the key means this).
# "Signal candle close" = the normal close of the candle the entry is made on, so the targets do not depend on the fill.
TP_FROM_ENTRY = "Entry price"
TP_FROM_SIGNAL = "Signal candle close"
TP_FROMS = (TP_FROM_ENTRY, TP_FROM_SIGNAL)
# STOP LOSS choices (owner 2026-10-26: "Add all these options in the SL"): worked out once at entry, then fixed.
SL_USD = "Fixed $ amount"
SL_STRUCT = "Structure (swing low / high)"
SL_CHAND = "Chandelier"
SL_MA = "Moving average"
SL_BASES = (BASIS_ATR, BASIS_PRICE, SL_USD, SL_STRUCT, SL_CHAND, SL_MA)

# TRAILING STOP types (owner rule 2026-10-23). The Trailing Stop replaces the Stop Loss once it takes charge and
# only ever tightens. Step moves LIVE on the price; the other two get a new level only when a candle closes.
TRAIL_STEP = "Step"
TRAIL_LLHH = "Lowest low / highest high"
TRAIL_MA = "Moving average"
# 4th type (owner 2026-10-28): the stop sits "Distance (% of ATR)" beyond the high / low of the signal candle (long: low
# minus the distance, short: high plus it); the distance is the ATR of the signal candle, fixed for the trade. At every
# candle close it is worked out again from the candle that just closed and only ever moves nearer (into profit).
TRAIL_CANDLE = "Signal candle high / low +/- ATR"
# 5th type (owner 2026-11-04): the stop follows the Supertrend line. At every candle close it is set at the line of the
# candle that just closed, and it only ever tightens (like the Moving average type, a line on the wrong side of the
# price does not take charge).
TRAIL_SUP = "Supertrend line"
TRAIL_TYPES = (TRAIL_STEP, TRAIL_LLHH, TRAIL_MA, TRAIL_CANDLE, TRAIL_SUP)
# Owner 2026-11-05 ("Both"): when the "Signal candle high / low +/- ATR" trail may take charge. A plan without the key =
# "Tighter than the Stop Loss" (how it always worked). "Only once in profit" = only after a candle CLOSES at least
# candle_profit (% of the signal candle ATR) beyond the entry price; from then on it only tightens.
TCS_TIGHTER = "Tighter than the Stop Loss"
TCS_PROFIT = "Only once in profit"
TCS_CHOICES = (TCS_TIGHTER, TCS_PROFIT)
# Owner 2026-11-05 ("Both"): how the Stop Loss "Structure" level is found. A plan without the key = the lowest low /
# highest high of the last N candles (how it always worked).
SLS_EXTREME = "Lowest low / highest high of N candles"
SLS_SWING = "Real swing point (N candles each side)"
SLS_CHOICES = (SLS_EXTREME, SLS_SWING)
START_SL = "Same as Stop Loss"
START_OWN = "Own value"
START_BEGIN = "Begins at a price move"   # 2026-10-24: Begins when the price moves / Stop goes to / Then moves every
TRAIL_STARTS = (START_SL, START_OWN, START_BEGIN)
TRAIL_MAS = ("MA 1", "MA 2")

# How many CLOSED candles the live bot loads for the hub (indicators are fully warmed up).
HUB_HISTORY_CANDLES = 1000

BLOCKS = ("candle", "ma1", "ma2", "mx", "adx", "ci", "rsi", "macd", "vol", "atr", "st", "bb", "sup", "eb", "sar", "sr")

BLOCK_TITLES = {
    "candle": "Candle colour",
    "ma1": "MA 1",
    "ma2": "MA 2",
    "mx": "MA Crossover",
    "adx": "ADX / DI",
    "ci": "Choppiness (CI)",
    "rsi": "RSI",
    "macd": "MACD",
    "vol": "Volume",
    "atr": "ATR",
    "st": "Structure break",
    "bb": "Bollinger Bands",       # 2026-11-04
    "sup": "Supertrend",           # 2026-11-04
    "sr": "Support / Resistance",  # 2026-11-05 (its settings are in plan["sr"])
    "eb": "EMA Band",              # 2026-11-05
    "sar": "Parabolic SAR",        # 2026-11-05
}

# --------------------------------------------------------------------------
# PLAN SCHEMA: block -> {param: kind}. kind: b=bool, i=int(>=min), f=float(>=min), s=choice
# --------------------------------------------------------------------------
# RSI and MACD conditions (owner 2026-10-25: "add those strategy in these indicators")
RSI_LEVEL = "Above / below level"
RSI_OBOS = "Overbought / oversold"
RSI_DIV = "Divergence"
RSI_MACROSS = "RSI crosses its own MA"      # 2026-11-01: long = RSI crosses UP through its own MA, short = DOWN
RSI_MODES = (RSI_LEVEL, RSI_OBOS, RSI_DIV, RSI_MACROSS)
RSI_KEYS = {RSI_LEVEL: ("length", "level"), RSI_OBOS: ("length", "oversold", "overbought", "n"),
            RSI_DIV: ("length", "pivot", "lookback", "n"),
            RSI_MACROSS: ("length", "ma_type", "ma_length", "ma_alma_offset", "ma_alma_sigma", "n")}
MACD_HIST = "Histogram above / below 0"
MACD_ZERO = "Zero line"
MACD_MODES = (MACD_HIST, MACD_ZERO)

# CONDITION TICKS (owner 2026-10-25: "all options must be an option for me to check their boxes"): MA 1, MA 2,
# MA Crossover, RSI and MACD take one or more conditions (plan key "modes", a list); ALL ticked must agree.
MODE_CHOICES = {"ma1": None, "ma2": None, "mx": None, "rsi": RSI_MODES, "macd": MACD_MODES}   # MA ones set below
# COUNTER TREND decided by (owner 2026-10-25): the EMA length box (as before) or any indicator that gives a side.
# BOLLINGER (owner 2026-11-04): long when the chart candle's LOW touches the bottom limit (short: the HIGH touches the top
# limit) on the signal candle or within the last N candles. The middle line is a simple moving average of the chart close.
# As the counter-trend decider ("Bollinger half") a LONG is with the trend in the lower half of the band and against it in
# the upper half, a SHORT the other way round; the middle line itself counts as against (the smaller size).
# SUPERTREND (owner 2026-11-04): ATR length and factor; Trend direction = long while the line is below the price;
# Fresh flip = the trend turned that way on the signal candle or within the last N candles.
SUP_TREND = "Trend direction"
SUP_FLIP = "Fresh flip within N candles"
SUP_MODES = (SUP_TREND, SUP_FLIP)
# EXIT WHEN (owner 2026-11-04, optional function, OFF by default): closes the open trade at a candle close when the moving
# average(s) curve against it (long: the value after the candle close is LOWER than the one before, short: HIGHER).
EXIT_BOTH = "Both MA 1 and MA 2 curve against the trade"
EXIT_MA2 = "MA 2 only (the slower one) curves against the trade"
EXIT_SIGNALS = (EXIT_BOTH, EXIT_MA2)
EXIT_WAIT = "Close and wait for a fresh signal"
EXIT_FLIP = "Close and flip to the other side"
EXIT_AFTERS = (EXIT_WAIT, EXIT_FLIP)
CT_EMA = "EMA"
CT_BLOCKS = ("ma1", "ma2", "mx", "adx", "rsi", "macd", "candle", "st", "bb", "eb", "sar")
CT_SOURCES = (CT_EMA,) + CT_BLOCKS

_MA = {"type": ("s", MA_TYPES), "length": ("i", 1), "modes": ("m", MA_MODES), "n": ("i", 0),
       "alma_offset": ("f", None), "alma_sigma": ("f", 0.0001)}
SCHEMA: dict[str, dict] = {
    "candle": {"strong": ("b", None), "prev_opposite": ("b", None)},
    "ma1": dict(_MA),
    "ma2": dict(_MA),
    "mx": {"modes": ("m", MX_MODES), "n": ("i", 0)},
    "adx": {"di_length": ("i", 1), "smoothing": ("i", 1), "level": ("f", 0.0), "di_required": ("b", None),
            "rising": ("b", None)},
    "ci": {"length": ("i", 2), "max": ("f", 0.0)},
    "rsi": {"modes": ("m", RSI_MODES), "length": ("i", 1), "level": ("f", 1.0), "oversold": ("f", 0.0),
            "overbought": ("f", 0.0), "n": ("i", 0), "pivot": ("i", 1), "lookback": ("i", 2),
            "ma_type": ("s", MA_TYPES), "ma_length": ("i", 1), "ma_alma_offset": ("f", None),
            "ma_alma_sigma": ("f", 0.0001)},
    "macd": {"modes": ("m", MACD_MODES), "fast": ("i", 1), "slow": ("i", 2), "signal": ("i", 1), "rising": ("b", None)},
    "vol": {"length": ("i", 1), "mult": ("f", 0.1), "exclude_current": ("b", None)},
    "atr": {"length": ("i", 1), "avg_length": ("i", 1), "mult": ("f", 0.1), "rising": ("b", None)},
    "st": {"length": ("i", 1)},
    "bb": {"length": ("i", 2), "mult": ("f", 0.0001), "n": ("i", 0)},
    "sup": {"modes": ("m", SUP_MODES), "atr_length": ("i", 1), "factor": ("f", 0.0001), "n": ("i", 0)},
    # EMA BAND (owner 2026-11-05): 3 EMAs of the same length on the chart HIGH (top), CLOSE (middle, drawn only) and LOW
    # (bottom). Four ticks pick the crossings that count: LONG when the close crosses ABOVE the bottom and / or the top
    # line, SHORT when it crosses BELOW the top and / or the bottom line (any ticked one is enough). A crossing counts
    # when the close is on the new side and was on the other side of THAT line within the last N candles (N = 1: the candle before).
    "eb": {"length": ("i", 1), "n": ("i", 1), "long_low": ("b", None), "long_high": ("b", None),
           "short_high": ("b", None), "short_low": ("b", None)},
    # PARABOLIC SAR (owner 2026-11-05): step (start and increase) and maximum; LONG while the dots are below the close,
    # SHORT while they are above it.
    "sar": {"step": ("f", 0.0001), "limit": ("f", 0.0001)},
    "sr": {},     # no numbers here: the Support / Resistance settings live in plan["sr"] (see SR_FIELDS)
}


# Option ticks added 2026-10-25: an older plan does not have them, they are then OFF (as before).
OPTIONAL_KEYS = {("candle", "strong"), ("candle", "prev_opposite"), ("adx", "rising"), ("atr", "rising"),
                 ("vol", "exclude_current"), ("eb", "long_low"), ("eb", "long_high"), ("eb", "short_high"),
                 ("eb", "short_low")}
SL_MISSING_MSG = "No SL was set for this trade. Set it manually."          # owner 2026-10-26: the trade still opens
SL_NOT_WORKED_MSG = "SL could not be worked out for this trade. Set it manually."


MODE_CHOICES.update(ma1=MA_MODES, ma2=MA_MODES, mx=MX_MODES)


# SUPPORT / RESISTANCE (owner 2026-11-05). plan["sr"] is optional: a plan without it = OFF. Every choice is a tick or a
# number he keys in; nothing is pre-selected. The engine is app/sr_engine.py.
SR_TICKS = ("find_hhll", "find_swing", "diagonal", "zone_on", "stop_zone_on", "bounce_long", "bounce_short",
            "bounce_edges", "bx_other", "bx_mid", "bx_break", "brk_long", "brk_short", "brk_close_on", "brk_vol_on",
            "brk_exit_inside", "retest_long", "retest_short", "wait_fail", "wait_newrange", "wait_runaway",
            "wait_none", "alone", "exit_sr_only", "exit_all")
# inert values for numbers that are not used (they never change a result)
SR_NUMBERS = {"hhll_n": 20, "swing_n": 5, "count": 1, "diag_points": 2, "zone_atr_pct": 25.0, "stop_zone_atr_pct": 25.0,
              "atr_length": 14, "bounce_edge_pct": 25.0, "brk_close_atr_pct": 10.0, "brk_vol_mult": 1.0,
              "brk_vol_len": 20, "wait_runaway_atr_pct": 100.0}


def sr_cfg(plan: dict) -> dict | None:
    """The completed Support / Resistance settings, or None when it is OFF."""
    sr = (plan or {}).get("sr")
    if not isinstance(sr, dict) or not sr.get("on"):
        return None
    return sr


def sr_exit_ticked(sr: dict | None) -> bool:
    return bool(sr) and any(sr.get(k) for k in ("bx_other", "bx_mid", "bx_break", "brk_exit_inside"))


def sr_entry_ticked(sr: dict | None) -> bool:
    return bool(sr) and any(sr.get(k) for k in ("bounce_long", "bounce_short", "brk_long", "brk_short",
                                                "retest_long", "retest_short"))


def sr_stop_on(plan: dict) -> bool:
    sr = sr_cfg(plan)
    return bool(sr and sr.get("stop_zone_on"))


class PlanError(ValueError):
    """The strategy plan is incomplete or invalid. The pair cannot start."""


def _need(d: dict, key: str, where: str):
    if not isinstance(d, dict) or key not in d or d[key] is None:
        raise PlanError(f"{where}: '{key}' is not set")
    return d[key]


def _check(value, kind, arg, where: str):
    if kind == "b":
        if not isinstance(value, bool):
            raise PlanError(f"{where}: must be on or off")
    elif kind == "i":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise PlanError(f"{where}: must be a whole number")
        if arg is not None and value < arg:
            raise PlanError(f"{where}: must be at least {arg}")
    elif kind == "f":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PlanError(f"{where}: must be a number")
        if arg is not None and value < arg:
            raise PlanError(f"{where}: must be at least {arg}")
    elif kind == "s":
        if value not in arg:
            raise PlanError(f"{where}: must be one of {list(arg)}")
    elif kind == "m":
        if not isinstance(value, list) or not value:
            raise PlanError(f"{where}: tick at least one condition")
        if len(set(value)) != len(value) or any(v not in arg for v in value):
            raise PlanError(f"{where}: each condition must be one of {list(arg)}, once")


def upgrade_plan(plan: dict) -> dict:
    """A COPY of a saved plan in today's format, so a plan saved before 2026-10-23 still loads and runs:
    Stop Loss OFF, trailing type "Lowest low / highest high", take-profit distances given as an ATR multiple are
    turned into % of ATR (1.5 -> 150), and "Allow Mid-Trend Re-entry" OFF (owner decision: not allowed by
    default). Nothing a plan already holds is changed. (The stop ladder rules of 2026-10-23 apply to it too.)"""
    if not isinstance(plan, dict):
        return plan
    p = copy.deepcopy(plan)
    p.setdefault("allow_reentry", False)
    if p.get("flip_action") in (None, FLIP_CLOSE_ONLY_OLD):
        p["flip_action"] = FLIP_ACTIONS[0]          # 2026-10-25: a flip always opens the new side
    if not isinstance(p.get("flip_rules"), dict):
        p["flip_rules"] = {"same_entry": False, "same_hold": True, "same_wait": True}   # an older plan
    ind = p.get("ind")
    if isinstance(ind, dict):
        for nb in ("bb", "sup", "eb", "sar", "sr"):    # 2026-11-04/05: Bollinger, Supertrend, Support / Resistance; a plan saved before has them OFF
            if nb not in ind:
                ind[nb] = {"entry": False, "flip": False}
    if isinstance(ind, dict):     # 2026-10-25: RSI / MACD got a Condition; an older plan keeps what it did
        if isinstance(ind.get("rsi"), dict):
            ind["rsi"].setdefault("mode", RSI_LEVEL)
        if isinstance(ind.get("macd"), dict):
            ind["macd"].setdefault("mode", MACD_HIST)
        # 2026-10-25: one condition ("mode") became ticks ("modes"); an older plan's condition is the one ticked
        for b in MODE_CHOICES:
            blk = ind.get(b)
            if isinstance(blk, dict):
                if "modes" not in blk and blk.get("mode") is not None:
                    blk["modes"] = [blk["mode"]]
                blk.pop("mode", None)
    if p.get("sr") is None:
        p["sr"] = {"on": False}       # Support / Resistance (2026-11-05): plans saved before it have it off
    sz = p.get("sizing")
    if isinstance(sz, dict) and isinstance(sz.get("counter"), dict):
        sz["counter"].setdefault("by", CT_EMA)      # an older plan: the EMA length decides (as before)
    if p.get("sl") is None:
        p["sl"] = {"on": False}
    if p.get("be") is None:
        p["be"] = {"on": False}       # Break-Even tab (2026-10-26): plans saved before it have it off
    tr = p.get("trail")
    if isinstance(tr, dict):
        tr.setdefault("type", TRAIL_LLHH)
    tp = p.get("tp")
    if isinstance(tp, dict):
        levels = tp.get("levels")
        old_style = isinstance(levels, list) and any(
            isinstance(lv, dict) and "mult" in lv and "value" not in lv for lv in levels)
        # An old plan measured its targets in ATR. A new plan with take profit ON must name its basis itself.
        if old_style or not tp.get("on"):
            tp.setdefault("basis", BASIS_ATR)
        if isinstance(levels, list):
            for lv in levels:
                if isinstance(lv, dict) and "value" not in lv and "mult" in lv:
                    m = lv.pop("mult")
                    lv["value"] = round(float(m) * 100.0, 9) if isinstance(m, (int, float)) and not isinstance(m, bool) else m
    return p


def flip_same_entry(plan: dict) -> bool:
    return bool((plan.get("flip_rules") or {}).get("same_entry"))


def effective_hold(plan: dict) -> dict:
    """The hold rule a flip uses: the Hold rule tab (Same as hold rule = Yes) or the flip's own."""
    fr = plan.get("flip_rules") or {}
    if fr.get("same_hold", True):
        return plan.get("hold") or {"on": False}
    return {"on": bool(fr.get("hold_on")), "adx_level": fr.get("hold_level")}


def flip_wait(plan: dict) -> int:
    """The wait (candles) of a flip's new side: the Entry wait (Same as entry wait = Yes) or the flip's own."""
    fr = plan.get("flip_rules") or {}
    if fr.get("same_wait", True):
        return int(plan["entry_wait"])
    return int(fr.get("wait") or 0)


def exit_cfg(plan: dict) -> dict | None:
    """The Exit when function of the plan, or None when it is off / not in the plan (an older plan)."""
    ew = (plan or {}).get("exit_when")
    if isinstance(ew, dict) and ew.get("on") is True:
        return ew
    return None


def _needed_blocks(plan: dict) -> set:
    """Blocks whose settings must be filled in: any block ticked for ENTRY or FLIP, the two MAs when
    'MA Crossover' is ticked, ADX when the hold rule is on (it uses the ADX settings), and the MA a
    "Moving average" Trailing Stop follows."""
    ind = plan["ind"]
    own_flip = not flip_same_entry(plan)
    need = {b for b in BLOCKS if ind[b]["entry"] or (own_flip and ind[b]["flip"])}
    if "mx" in need:
        need |= {"ma1", "ma2"}
    if effective_hold(plan).get("on"):
        need.add("adx")
    ct = (plan.get("sizing") or {}).get("counter") or {}
    if ct.get("on") and ct.get("by", CT_EMA) in CT_BLOCKS:
        need.add(ct["by"])                         # the indicator that decides the counter trend
        if ct["by"] == "mx":
            need |= {"ma1", "ma2"}
    slp = plan.get("sl") or {}
    if slp.get("on") and slp.get("basis") == SL_MA:
        need.add("ma2" if slp.get("ma") == "MA 2" else "ma1")      # the MA the Stop Loss is set at
    tr = plan.get("trail") or {}
    if tr.get("on") and tr.get("type") == TRAIL_MA:
        need.add("ma1" if tr.get("ma") == "MA 1" else "ma2" if tr.get("ma") == "MA 2" else "ma1")
    if tr.get("on") and tr.get("type") == TRAIL_SUP:
        need.add("sup")                            # the Supertrend line the Trailing Stop follows
    ew = exit_cfg(plan)
    if ew:
        need.add("ma2")                            # Exit when reads the MA numbers
        if ew.get("signal") != EXIT_MA2:
            need.add("ma1")
    return need


def _validate_sr(plan: dict, ind: dict) -> None:
    sr = plan.get("sr")
    if not isinstance(sr, dict):
        raise PlanError("Support / Resistance: wrong format")
    _check(_need(sr, "on", "Support / Resistance"), "b", None, "Support / Resistance switch")
    used_in_combine = ind["sr"]["entry"] or ind["sr"]["flip"]
    if not sr["on"]:
        if used_in_combine:
            raise PlanError("Support / Resistance is ticked in the indicator list but the Support / Resistance "
                            "function is switched off. Switch it on or untick it.")
        return
    t = "Support / Resistance"
    for k in SR_TICKS:
        if sr.get(k) is not None:
            _check(sr[k], "b", None, f"{t}: {k}")

    def on(k):
        return bool(sr.get(k))

    if not (on("find_hhll") or on("find_swing") or on("diagonal")):
        raise PlanError(f"{t}: tick at least one way to find the range (highest high / lowest low, swing points, "
                        "or a diagonal line)")
    if on("find_hhll"):
        _check(_need(sr, "hhll_n", t), "i", 2, f"{t}: highest high / lowest low of the last N candles (N)")
    if on("find_swing") or on("diagonal"):
        _check(_need(sr, "swing_n", t), "i", 1, f"{t}: swing point candles on each side (N)")
    _check(_need(sr, "count", t), "i", 1, f"{t}: how many nearest tops / bottoms")
    if on("diagonal"):
        _check(_need(sr, "diag_points", t), "i", 2, f"{t}: diagonal line points (2 or 3)")
        if sr["diag_points"] > 3:
            raise PlanError(f"{t}: the diagonal line goes through 2 or 3 swing points")
    if on("zone_on"):
        _check(_need(sr, "zone_atr_pct", t), "f", 0.0, f"{t}: zone width (% of ATR)")
    if on("stop_zone_on"):
        _check(_need(sr, "stop_zone_atr_pct", t), "f", 0.0, f"{t}: stop outside the zone (% of ATR)")
    if on("bounce_edges"):
        _check(_need(sr, "bounce_edge_pct", t), "f", 0.0001, f"{t}: edge width (% of the range)")
        if sr["bounce_edge_pct"] > 50:
            raise PlanError(f"{t}: edge width: must be 50 or less (% of the range, measured from each end)")
        if not (on("bounce_long") or on("bounce_short")):
            raise PlanError(f"{t}: 'Edges only' belongs to the bounce entries - tick Long and / or Short bounce, or untick it")
    if on("brk_close_on"):
        _check(_need(sr, "brk_close_atr_pct", t), "f", 0.0, f"{t}: close beyond the level by (% of ATR)")
    if on("brk_vol_on"):
        _check(_need(sr, "brk_vol_mult", t), "f", 0.1, f"{t}: volume above the average by (times)")
        _check(_need(sr, "brk_vol_len", t), "i", 1, f"{t}: volume average length (candles)")
    if on("wait_runaway"):
        _check(_need(sr, "wait_runaway_atr_pct", t), "f", 0.0001, f"{t}: price runs away (% of ATR)")
    needs_atr = on("zone_on") or on("stop_zone_on") or on("brk_close_on") or on("wait_runaway")
    if needs_atr:
        _check(_need(sr, "atr_length", t), "i", 1, f"{t}: ATR length")
    entry_ticked = sr_entry_ticked(sr)
    used_for_entry = used_in_combine or on("alone")
    if used_for_entry and not entry_ticked:
        raise PlanError(f"{t} is used for entries but no entry is ticked. Tick Bounce, Breakout and / or Retest "
                        "(Long / Short), or stop using it for entries.")
    if on("alone") and not entry_ticked:
        raise PlanError(f"{t}: stand-alone entries need an entry ticked")
    if entry_ticked and not any(on(k) for k in ("wait_fail", "wait_newrange", "wait_runaway", "wait_none")) and (
            on("retest_long") or on("retest_short") or (
            (on("bounce_long") or on("bounce_short")) and (on("brk_long") or on("brk_short")))):
        raise PlanError(f"{t}: tick what ends the wait for the retest (the level fails, a new range forms, price "
                        "runs away, or nothing ends it)")
    if sr_exit_ticked(sr) and not (on("exit_sr_only") or on("exit_all")):
        raise PlanError(f"{t}: tick where the exits apply (Support / Resistance trades only, and / or every trade)")


def validate_plan(plan: dict) -> dict:
    """Returns the plan unchanged if it is complete and valid, else raises PlanError. It is checked in today's
    format (see upgrade_plan), so a plan saved before 2026-10-23 is still accepted.
    There are no defaults: a value that is NEEDED and missing is an error, never silently filled.
    What is needed depends on what is switched on: an indicator that is OFF for both entry and flip needs
    no numbers; the hold rule, the max-loss %, take profit, its ticked levels, the trailing look-back and
    the Renko ATR length are needed only when they are used."""
    if not isinstance(plan, dict):
        raise PlanError("no strategy plan is set on this pair")
    original = plan
    plan = upgrade_plan(plan)          # checked in today's format; the caller's plan is not changed
    _check(_need(plan, "allow_reentry", "plan"), "b", None, "Allow Mid-Trend Re-entry")
    chart = _need(plan, "chart_type", "plan")
    _check(chart, "s", CHART_TYPES, "chart type")
    if chart == "Renko":
        _check(_need(plan, "renko_atr_length", "plan"), "i", 1, "Renko ATR length")
    _check(_need(plan, "combine_entry", "plan"), "s", COMBINE, "Indicator Combination for ENTRY")
    fr = _need(plan, "flip_rules", "plan")
    for k, t in (("same_entry", "Flip: Same as entry"), ("same_hold", "Flip: Same as hold rule"),
                 ("same_wait", "Flip: Same as entry wait")):
        _check(_need(fr, k, "flip"), "b", None, t + " (Yes / No)")
    if not fr["same_hold"]:
        _check(_need(fr, "hold_on", "flip"), "b", None, "Flip: own hold rule on / off")
        if fr["hold_on"]:
            _check(_need(fr, "hold_level", "flip"), "f", 0.0, "Flip: own hold rule ADX level")
    if not fr["same_wait"]:
        _check(_need(fr, "wait", "flip"), "i", 0, "Flip: own wait (candles)")
    if not fr["same_entry"]:
        _check(_need(plan, "combine_flip", "plan"), "s", COMBINE, "Indicator Combination for FLIP")
    _check(_need(plan, "flip_action", "plan"), "s", FLIP_ACTIONS, "flip action")
    _check(_need(plan, "entry_wait", "plan"), "i", 0, "entry wait")
    _check(_need(plan, "wait_first_reversal", "plan"), "b", None, "first trade only on a fresh reversal")

    ind = _need(plan, "ind", "plan")
    for b in BLOCKS:
        blk = _need(ind, b, "indicators")
        title = BLOCK_TITLES[b]
        _check(_need(blk, "entry", title), "b", None, f"{title} / entry switch")
        _check(_need(blk, "flip", title), "b", None, f"{title} / flip switch")
    cb = ind["candle"]
    if chart == "Candles" and (cb["entry"] or cb["flip"]):
        raise PlanError("Candle colour works only with Heikin Ashi or Renko: on normal Candles it cannot show the trend. "
                        "Untick it or change the chart type.")

    # "At least N": N is a whole number from 1 up to the number of indicators ticked for that side.
    for which, key, nkey, title in (("entry", "combine_entry", "entry_min", "ENTRY"),
                                    ("flip", "combine_flip", "flip_min", "FLIP")):
        if which == "flip" and fr["same_entry"]:
            continue
        if plan[key] == COMBINE_ATLEAST:
            ticked = sum(1 for b in BLOCKS if ind[b][which])
            _check(_need(plan, nkey, "plan"), "i", 1, f"{title}: how many indicators must agree (N)")
            if plan[nkey] > ticked:
                raise PlanError(f"{title}: N is {plan[nkey]:g} but only {ticked} indicator(s) are ticked for {title}")

    # MAIN (owner 2026-10-24): one indicator per side he picks as the Main; it has to happen, the other ticked
    # indicators show confluence. ENTRY and FLIP each have their own (or none). Not set = as before.
    for which, key, title in (("entry", "main_entry", "ENTRY"), ("flip", "main_flip", "FLIP")):
        m = plan.get(key)
        if m is None or (which == "flip" and fr["same_entry"]):
            continue
        if m not in BLOCKS:
            raise PlanError(f"Main for {title}: unknown indicator {m!r}")
        if not ind[m][which]:
            raise PlanError(f"Main for {title}: {BLOCK_TITLES[m]} is not ticked for {title}. Tick it or choose another Main.")

    name = plan.get("name")
    if name is not None:
        if not isinstance(name, str) or len(name.strip()) == 0 or len(name) > 60:
            raise PlanError("strategy name: must be text of 1 to 60 characters (or left out)")

    hold = _need(plan, "hold", "plan")
    _check(_need(hold, "on", "hold rule"), "b", None, "hold rule switch")
    if hold["on"]:
        _check(_need(hold, "adx_level", "hold rule"), "f", 0.0, "hold rule ADX level")

    needed = _needed_blocks(plan)
    own_flip = not fr["same_entry"]
    for b in BLOCKS:
        if b not in needed:
            continue
        blk = ind[b]
        title = BLOCK_TITLES[b]
        for key, (kind, arg) in SCHEMA[b].items():
            # ALMA numbers only matter for an ALMA line; N only for a "crossed within N candles" condition.
            if b in ("ma1", "ma2") and key in ("alma_offset", "alma_sigma") and blk.get("type") != "ALMA":
                continue
            modes = blk.get("modes") if isinstance(blk.get("modes"), list) else []
            if key == "n" and b in ("ma1", "ma2", "mx") and not any(
                    "crossed" in str(m) or m == MA_PULLBACK for m in modes):
                continue
            # Bollinger / Supertrend: the conditions (and N) matter only when the block gives a signal (ticked for
            # ENTRY / FLIP, or the Bollinger half for the counter trend needs no N); the Supertrend line of the
            # Trailing Stop needs only its ATR length and factor.
            gives_signal = ind[b]["entry"] or (own_flip and ind[b]["flip"])
            if b == "bb" and key == "n" and not gives_signal:
                continue
            if b == "sup" and key == "modes" and not gives_signal:
                continue
            if b == "sup" and key == "n" and not (gives_signal and SUP_FLIP in modes):
                continue
            # the RSI average's ALMA numbers only matter when that average is an ALMA
            if b == "rsi" and key in ("ma_alma_offset", "ma_alma_sigma") and blk.get("ma_type") != "ALMA":
                continue
            if (b, key) in OPTIONAL_KEYS and blk.get(key) is None:
                continue
            if kind == "m" and not blk.get(key):
                raise PlanError(f"{title}: tick at least one condition")
            if b == "rsi" and key != "modes" and not any(key in RSI_KEYS.get(m, ()) for m in modes):
                continue
            _check(_need(blk, key, title), kind, arg, f"{title} / {key}")
    if "eb" in needed and not any(ind["eb"].get(k) for k in ("long_low", "long_high", "short_high", "short_low")):
        raise PlanError("EMA Band: tick at least one line crossing (long: bottom / top line, short: top / bottom line)")
    if "sar" in needed and ind["sar"]["limit"] < ind["sar"]["step"]:
        raise PlanError("Parabolic SAR: the maximum must be the same as or larger than the step")
    if "macd" in needed and ind["macd"]["slow"] <= ind["macd"]["fast"]:
        raise PlanError("MACD: slow must be larger than fast")
    if "rsi" in needed and RSI_OBOS in ind["rsi"]["modes"]:
        r = ind["rsi"]
        if not (0 < r["oversold"] < r["overbought"] < 100):
            raise PlanError("RSI overbought / oversold: needs 0 < Oversold < Overbought < 100 (e.g. 30 and 70)")
    if "rsi" in needed and RSI_DIV in ind["rsi"]["modes"] and ind["rsi"]["lookback"] <= ind["rsi"]["pivot"]:
        raise PlanError("RSI divergence: Max look-back must be bigger than Pivot candles")

    sz = _need(plan, "sizing", "plan")
    method = sz.get("method", SIZE_ALLOC)
    _check(method, "s", SIZE_METHODS, "entry size method")
    if method == SIZE_FIXED:
        _check(_need(sz, "base_amount", "sizing"), "f", 0.01, "base amount (USDT)")
    else:
        _check(_need(sz, "alloc_pct", "sizing"), "f", 0.1, "allocation %")
        if sz["alloc_pct"] > 100:
            raise PlanError("sizing: allocation % must be 100 or less")
    _check(_need(sz, "leverage", "sizing"), "i", 1, "leverage")
    if sz["leverage"] > 125:
        raise PlanError("sizing: leverage must be 125 or less")
    # Lock-out % (optional, fixed base only; absent = OFF = the next entry carries the whole total).
    lock = sz.get("lock_pct")
    if lock is not None:
        if method != SIZE_FIXED:
            raise PlanError("sizing: the lock-out % works only with the fixed base amount")
        _check(lock, "f", 0.0, "% of last profit added to the next entry")
        if lock > 100:
            raise PlanError("sizing: % of last profit added to the next entry must be 100 or less")
    if sz.get("margin_alert") is not None:
        _check(sz["margin_alert"], "b", None, "email alert on margin")
    _check(_need(sz, "stop_on", "sizing"), "b", None, "max loss switch")
    if sz["stop_on"]:
        basis = sz.get("stop_basis", STOP_BAL)
        _check(basis, "s", STOP_BASES, "max loss basis")
        if basis == STOP_USD:
            _check(_need(sz, "stop_usd", "sizing"), "f", 0.01, "max loss per trade (USDT)")
        else:
            _check(_need(sz, "stop_pct", "sizing"), "f", 0.1, "max loss per trade %")
            if sz["stop_pct"] > 100:
                raise PlanError("sizing: max loss per trade must be 100% or less")

    # Trend / counter-trend sizing (optional box; absent = OFF): a trade WITH the trend (chart close on the
    # right side of the EMA) uses the size above; a trade AGAINST it uses these.
    ct = sz.get("counter")
    if ct is not None:
        _check(_need(ct, "on", "counter-trend sizing"), "b", None, "counter-trend sizing switch")
        if ct["on"]:
            by = ct.get("by", CT_EMA)
            _check(by, "s", CT_SOURCES, "counter trend decided by")
            if by == CT_EMA:
                _check(_need(ct, "ema_length", "counter-trend sizing"), "i", 1, "counter-trend sizing EMA length")
                if ct["ema_length"] > 500:
                    raise PlanError("counter-trend sizing: EMA length must be 500 or less")
            elif by == "candle" and chart == "Candles":
                raise PlanError("counter trend decided by Candle colour works only with Heikin Ashi or Renko")
            # 0 = no counter-trend entry at all (owner 2026-10-25)
            if method == SIZE_FIXED:
                _check(_need(ct, "base_amount", "counter-trend sizing"), "f", 0.0, "counter-trend base amount (USDT)")
            else:
                _check(_need(ct, "alloc_pct", "counter-trend sizing"), "f", 0.0, "counter-trend allocation %")
                if ct["alloc_pct"] > 100:
                    raise PlanError("counter-trend sizing: allocation % must be 100 or less")
            _check(_need(ct, "leverage", "counter-trend sizing"), "i", 1, "counter-trend leverage")
            if ct["leverage"] > 125:
                raise PlanError("counter-trend sizing: leverage must be 125 or less")

    # Shadow mode (optional box; absent = OFF): paper trades after a new tracker high.
    sh = plan.get("shadow")
    if sh is not None:
        _check(_need(sh, "on", "shadow mode"), "b", None, "shadow mode switch")
        if sh["on"]:
            _check(_need(sh, "trades", "shadow mode"), "i", 1, "shadow mode number of paper trades")
            if sh["trades"] > 100:
                raise PlanError("shadow mode: number of paper trades must be 100 or less")
            _check(_need(sh, "slippage_ticks", "shadow mode"), "i", 0, "shadow mode paper slippage ticks")
            _check(_need(sh, "tracker_start", "shadow mode"), "f", 0.000001, "shadow mode tracker starting amount")

    tp = _need(plan, "tp", "plan")
    _check(_need(tp, "on", "take profit"), "b", None, "take profit switch")
    if tp["on"]:
        _check(_need(tp, "basis", "take profit"), "s", BASES, "take profit: measured on")
        if "from" in tp:     # optional: a missing key means "Entry price" (plans saved before 2026-10-27)
            _check(tp["from"], "s", TP_FROMS, "take profit: measured from")
        if tp["basis"] == BASIS_ATR:
            _check(_need(tp, "atr_length", "take profit"), "i", 1, "take profit ATR length")
        levels = _need(tp, "levels", "take profit")
        if not isinstance(levels, list) or len(levels) != 3:
            raise PlanError("take profit: there must be exactly 3 levels (each can be switched off)")
        total = 0.0
        for k, lv in enumerate(levels, 1):
            _check(_need(lv, "on", f"TP {k}"), "b", None, f"TP {k} switch")
            if not lv["on"]:
                continue
            _check(_need(lv, "value", f"TP {k}"), "f", 0.0001, f"TP {k} distance (%)")
            _check(_need(lv, "pct", f"TP {k}"), "f", 1.0, f"TP {k} % of position")
            if lv["pct"] > 100:
                raise PlanError(f"TP {k}: % of position must be 100 or less")
            total += lv["pct"]
        if not any(lv["on"] for lv in levels):
            raise PlanError("take profit is ON but no TP level is ticked")
        if total > 100 + 1e-9:
            raise PlanError(f"take profit: the ticked TP percentages add up to {total:g} - they must be 100 or less")

    # STOP LOSS (owner 2026-10-26): always in the menu; a plan may be saved without it. Without an SL the trade still
    # opens and the owner gets a Telegram + email to set it manually.
    sl = _need(plan, "sl", "plan")
    _check(_need(sl, "on", "stop loss"), "b", None, "stop loss switch")
    if sl["on"]:
        _check(_need(sl, "basis", "stop loss"), "s", SL_BASES, "stop loss: measured on")
        b = sl["basis"]
        if b in (BASIS_ATR, BASIS_PRICE, SL_CHAND):
            _check(_need(sl, "value", "stop loss"), "f", 0.0001, "stop loss value (%)")
        if b in (BASIS_ATR, SL_CHAND):
            _check(_need(sl, "atr_length", "stop loss"), "i", 1, "stop loss ATR length")
        if b == SL_USD:
            _check(_need(sl, "usd", "stop loss"), "f", 0.01, "stop loss (USDT)")
        if sl.get("struct_mode") is not None:
            _check(sl["struct_mode"], "s", SLS_CHOICES, "stop loss: how the Structure level is found")
        if b == SL_STRUCT and sl.get("struct_mode") == SLS_SWING:
            _check(_need(sl, "struct_n", "stop loss"), "i", 1, "stop loss: swing point candles on each side (N)")
        elif b in (SL_STRUCT, SL_CHAND):
            _check(_need(sl, "length", "stop loss"), "i", 1, "stop loss: N candles")
        if b == SL_MA:
            _check(_need(sl, "ma", "stop loss"), "s", TRAIL_MAS, "stop loss: which MA")
        if sl.get("time_on") is not None:
            _check(sl["time_on"], "b", None, "stop loss: time stop switch")
            if sl["time_on"]:
                _check(_need(sl, "time_candles", "stop loss"), "i", 1, "time stop: N candles")

    # BREAK-EVEN (optional): the Stop Loss moves to the entry price after TP n and / or after the price moves X% of ATR.
    be = plan.get("be") or {"on": False}
    _check(_need(be, "on", "break-even"), "b", None, "break-even switch")
    if be["on"]:
        a_tp, a_atr = be.get("after_tp") is True, be.get("after_atr") is True
        if not (a_tp or a_atr):
            raise PlanError("Break-Even: tick After TP and / or After ATR")
        if a_tp:
            _check(_need(be, "tp_n", "break-even"), "i", 1, "Break-Even: After TP")
            n = be["tp_n"]
            tpp = plan.get("tp") or {}
            lvls = tpp.get("levels") or []
            if n > 3 or not tpp.get("on") or n > len(lvls) or not lvls[n - 1].get("on"):
                raise PlanError(f"Break-Even: After TP {n} - TP{n} is not used in Take profit")
        if a_atr:
            _check(_need(be, "atr_value", "break-even"), "f", 0.0001, "Break-Even: After ATR (%)")
            _check(_need(be, "atr_length", "break-even"), "i", 1, "Break-Even: ATR length")

    tr = _need(plan, "trail", "plan")
    _check(_need(tr, "on", "trailing stop"), "b", None, "trailing stop switch")
    if tr["on"]:
        _check(_need(tr, "type", "trailing stop"), "s", TRAIL_TYPES, "trailing stop type")
        if tr["type"] == TRAIL_LLHH:
            _check(_need(tr, "length", "trailing stop"), "i", 1, "trailing stop look-back")
        elif tr["type"] == TRAIL_MA:
            _check(_need(tr, "ma", "trailing stop"), "s", TRAIL_MAS, "trailing stop: which MA")
        elif tr["type"] == TRAIL_CANDLE:
            _check(_need(tr, "value", "trailing stop"), "f", 0.0001, "trailing stop: distance (% of ATR)")
            _check(_need(tr, "atr_length", "trailing stop"), "i", 1, "trailing stop ATR length")
            if tr.get("candle_start") is not None:
                _check(tr["candle_start"], "s", TCS_CHOICES, "trailing stop: when it may take charge")
                if tr["candle_start"] == TCS_PROFIT:
                    _check(_need(tr, "candle_profit", "trailing stop"), "f", 0.0001,
                           "trailing stop: in profit by (% of ATR)")
        elif tr["type"] == TRAIL_SUP:
            pass          # follows the Supertrend line: its ATR length and factor are the Supertrend numbers (Indicators)
        else:
            _check(_need(tr, "basis", "trailing stop"), "s", BASES, "trailing stop: measured on")
            _check(_need(tr, "start", "trailing stop"), "s", TRAIL_STARTS, "trailing stop start")
            if tr["start"] == START_SL:
                if not sl["on"]:
                    raise PlanError("Trailing Stop start is 'Same as Stop Loss' but Stop Loss is not picked - "
                                    "pick Stop Loss or choose 'Own value'")
            elif tr["start"] == START_BEGIN:
                _check(_need(tr, "begin_value", "trailing stop"), "f", 0.0001,
                       "trailing stop: Begins when the price moves (%)")
                _check(_need(tr, "lock_value", "trailing stop"), "f", None, "trailing stop: Stop goes to (%)")
                if tr["lock_value"] >= tr["begin_value"]:
                    raise PlanError("Trailing Stop: 'Stop goes to' must be lower than 'Begins when the price moves' "
                                    "(else the stop would be on the wrong side of the price when it begins)")
            else:
                _check(_need(tr, "start_value", "trailing stop"), "f", 0.0001, "trailing stop start value (%)")
            _check(_need(tr, "gap", "trailing stop"), "f", 0.0001, "trailing stop gap / then moves every (%)")
            if tr["basis"] == BASIS_ATR:
                _check(_need(tr, "atr_length", "trailing stop"), "i", 1, "trailing stop ATR length")

    # EXIT WHEN (optional, 2026-11-04): close the trade when the MA(s) curve against it. A plan without the key = OFF.
    ew = plan.get("exit_when")
    if ew is not None:
        if not isinstance(ew, dict):
            raise PlanError("Exit when: wrong format")
        _check(_need(ew, "on", "exit when"), "b", None, "Exit when switch")
        if ew["on"]:
            _check(_need(ew, "signal", "exit when"), "s", EXIT_SIGNALS, "Exit when: signal")
            _check(_need(ew, "after", "exit when"), "s", EXIT_AFTERS, "Exit when: after the close")

    # COOLDOWN AFTER A LOSS (optional, 2026-11-01): after a losing trade the pair waits N candles before it enters again.
    # "Losses in a row" is optional (left out = after every loss). A plan without the key means the cooldown is off.
    cd = plan.get("cooldown")
    if cd is not None:
        if not isinstance(cd, dict):
            raise PlanError("Cooldown after a loss: wrong format")
        _check(_need(cd, "on", "cooldown"), "b", None, "Cooldown after a loss switch")
        if cd["on"]:
            _check(_need(cd, "candles", "cooldown"), "i", 1, "Cooldown: candles to wait")
            if cd.get("after_losses") is not None:
                _check(cd["after_losses"], "i", 1, "Cooldown: losses in a row")

    # LOSS LIMIT of this strategy (optional, 2026-11-01): no NEW entry once the realized loss of the day / week of this
    # pair reaches the amount (USDT) or the % of the account balance at the start of that period (daily_pct /
    # weekly_pct, added 2026-11-02). At least one of the four is needed. A plan without the key means it is off.
    ll = plan.get("loss_limit")
    if ll is not None:
        if not isinstance(ll, dict):
            raise PlanError("Loss limit: wrong format")
        _check(_need(ll, "on", "loss limit"), "b", None, "Loss limit switch")
        if ll["on"]:
            if all(ll.get(k) is None for k in ("daily", "weekly", "daily_pct", "weekly_pct")):
                raise PlanError("Loss limit: key in a daily and / or a weekly amount (USDT or %)")
            for k, t in (("daily", "Daily"), ("weekly", "Weekly")):
                if ll.get(k) is not None:
                    _check(ll[k], "f", 0.01, f"Loss limit: {t} amount (USDT)")
                if ll.get(k + "_pct") is not None:
                    _check(ll[k + "_pct"], "f", 0.01, f"Loss limit: {t} % of the balance")
                    if ll[k + "_pct"] > 100:
                        raise PlanError(f"Loss limit: {t} % of the balance: must be 100 or less")

    # SUPPORT / RESISTANCE (optional, 2026-11-05)
    _validate_sr(plan, ind)

    # A plan that can never open a trade is refused (a hub with nothing ticked does nothing).
    sr_alone = bool(plan["sr"].get("on") and plan["sr"].get("alone"))      # Support / Resistance as its own strategy
    if not any(ind[b]["entry"] for b in BLOCKS) and not sr_alone:
        raise PlanError("no indicator is ticked for ENTRY - the plan could never open a trade")
    return original


# Values for settings that are NOT used (their switch is off). They never take part in any signal or order;
# they only let the one calculation run over every block. A test proves the results do not change with them.
_INERT_BLOCK = {
    "type": "SMA", "length": 14, "mode": None, "n": 1, "alma_offset": 0.85, "alma_sigma": 6.0,
    "di_length": 14, "smoothing": 14, "level": 20.0, "di_required": False,
    "max": 55.0, "fast": 12, "slow": 26, "signal": 9, "rising": False, "mult": 1.0,
    "avg_length": 20, "strong": False, "exclude_current": False, "oversold": 30.0, "overbought": 70.0, "pivot": 5, "lookback": 60,
    "ma_type": "SMA", "ma_length": 14, "ma_alma_offset": 0.85, "ma_alma_sigma": 6.0,
    "prev_opposite": False, "atr_length": 10, "factor": 3.0,
    "long_low": False, "long_high": False, "short_high": False, "short_low": False, "step": 0.02, "limit": 0.2,
}


def complete_plan(plan: dict) -> dict:
    """A validated plan as a COPY with the unused settings filled with inert values (see above), so all code
    can read every key. Raises PlanError if the plan itself is not valid."""
    validate_plan(plan)
    out = upgrade_plan(plan)       # a copy in today's format (see upgrade_plan)
    ind = out["ind"]
    for b in BLOCKS:
        for key, (kind, arg) in SCHEMA[b].items():
            if key in ind[b]:
                continue
            if key == "mode":
                ind[b][key] = arg[0]
            elif key == "modes":
                ind[b][key] = [arg[0]]
            else:
                ind[b][key] = _INERT_BLOCK[key]
    if "renko_atr_length" not in out:
        out["renko_atr_length"] = 14
    out["hold"].setdefault("adx_level", 25.0)
    out["sizing"].setdefault("stop_pct", 6.0)
    out["sizing"].setdefault("method", SIZE_ALLOC)
    out["sizing"].setdefault("alloc_pct", 1.0)          # inert when the method is the fixed base amount
    out["sizing"].setdefault("base_amount", 1.0)        # inert when the method is the allocation %
    out["sizing"].setdefault("stop_basis", STOP_BAL)
    out["sizing"].setdefault("stop_usd", 1.0)           # inert unless the basis is the fixed $ amount
    out["sizing"].setdefault("counter", {"on": False})
    out["sizing"].setdefault("lock_pct", None)          # None = lock-out OFF (the whole total is carried)
    out["sizing"].setdefault("margin_alert", False)
    out.setdefault("entry_min", 1)                      # inert unless the combine is "At least N"
    out.setdefault("combine_flip", COMBINE_ALL)        # inert when the flip is Same as entry
    out.setdefault("flip_min", 1)
    ct = out["sizing"]["counter"]
    ct.setdefault("by", CT_EMA)
    ct.setdefault("ema_length", 200)
    ct.setdefault("alloc_pct", out["sizing"]["alloc_pct"])
    ct.setdefault("base_amount", out["sizing"]["base_amount"])
    ct.setdefault("leverage", out["sizing"]["leverage"])
    sr = out["sr"]
    for k in SR_TICKS:
        sr.setdefault(k, False)
    for k, v_ in SR_NUMBERS.items():
        sr.setdefault(k, v_)
    out.setdefault("exit_when", {"on": False})
    out["exit_when"].setdefault("signal", EXIT_BOTH)         # inert while Exit when is off
    out["exit_when"].setdefault("after", EXIT_WAIT)
    out.setdefault("shadow", {"on": False})
    out["shadow"].setdefault("trades", 3)
    out["shadow"].setdefault("slippage_ticks", 0)
    out["shadow"].setdefault("tracker_start", 1.0)
    tp = out["tp"]
    tp.setdefault("basis", BASIS_ATR)
    tp.setdefault("atr_length", 14)
    if "levels" not in tp:
        tp["levels"] = [{"on": False, "value": 100.0, "pct": 1.0} for _ in range(3)]
    for lv in tp["levels"]:
        lv.setdefault("value", 100.0)
        lv.setdefault("pct", 1.0)
    sl = out["sl"]
    sl.setdefault("basis", BASIS_ATR)
    sl.setdefault("value", 100.0)
    sl.setdefault("atr_length", 14)
    sl.setdefault("usd", 1.0)            # inert unless the basis is Fixed $ amount
    sl.setdefault("length", 10)          # inert unless Structure / Chandelier
    sl.setdefault("struct_n", 5)         # inert unless Structure is found by "Real swing point"
    sl.setdefault("ma", "MA 1")          # inert unless Moving average
    sl.setdefault("time_on", False)
    sl.setdefault("time_candles", 1)
    be = out["be"]
    be.setdefault("after_tp", False)
    be.setdefault("tp_n", 1)
    be.setdefault("after_atr", False)
    be.setdefault("atr_value", 100.0)
    be.setdefault("atr_length", 14)
    tr = out["trail"]
    tr.setdefault("type", TRAIL_LLHH)
    tr.setdefault("length", 10)
    tr.setdefault("ma", "MA 1")
    tr.setdefault("basis", BASIS_ATR)
    tr.setdefault("start", START_OWN)
    tr.setdefault("start_value", 100.0)
    tr.setdefault("begin_value", 100.0)      # inert unless the start is "Begins at a price move"
    tr.setdefault("lock_value", 0.0)
    tr.setdefault("candle_profit", 100.0)   # inert unless the Signal candle trail starts "Only once in profit"
    tr.setdefault("gap", 50.0)
    tr.setdefault("atr_length", 14)
    return out


def plan_summary(plan: dict) -> str:
    """One-line description for logs / the dashboard."""
    ind = plan["ind"]
    e = [BLOCK_TITLES[b] for b in BLOCKS if (ind.get(b) or {}).get("entry")]
    f = [BLOCK_TITLES[b] for b in BLOCKS if (ind.get(b) or {}).get("flip")]
    ce = f"At least {plan['entry_min']:g}" if plan['combine_entry'] == COMBINE_ATLEAST else plan['combine_entry']
    cf = f"At least {plan['flip_min']:g}" if plan['combine_flip'] == COMBINE_ATLEAST else plan['combine_flip']
    me = f" | ENTRY main: {BLOCK_TITLES[plan['main_entry']]}" if plan.get("main_entry") in BLOCK_TITLES else ""
    mf = f" | FLIP main: {BLOCK_TITLES[plan['main_flip']]}" if plan.get("main_flip") in BLOCK_TITLES else ""
    sr = plan.get("sr") or {}
    srs = (" | Support / Resistance: " + ("its own strategy" if sr.get("alone") else "on")) if sr.get("on") else ""
    return (f"{plan['chart_type']} | ENTRY({ce}): {', '.join(e) or 'none'} | "
            f"FLIP({cf}): {', '.join(f) or 'none'} | wait {plan['entry_wait']}{me}{mf}{srs}")


# ==========================================================================
# COOLDOWN AFTER A LOSS (owner 2026-11-01)
# ==========================================================================
def cooldown_cfg(plan: dict) -> dict | None:
    """The cooldown of the plan, or None when it is off / not in the plan (an older plan)."""
    cd = (plan or {}).get("cooldown")
    if isinstance(cd, dict) and cd.get("on") is True:
        return cd
    return None


def cooldown_after_close(plan: dict, loss_streak: int, trade_result: float, last_processed_candle, candle_ms) -> tuple[int, int | None]:
    """A trade has just closed completely with this net result (USDT, all fills added up).
    Returns (new losses-in-a-row count, block_through) where block_through is the open time (ms) of the last SIGNAL
    candle whose entry is blocked, or None when no cooldown starts.

    A loss is a result below 0; a trade that ends at 0 or in profit ends the row. Counting: the candle closes after
    the close of the trade are skipped one by one; last_processed_candle is the open time of the last candle the bot
    had acted on when the trade closed, so the first close that counts belongs to the candle after it (an exit made
    at a candle close, like a flip, also blocks its own candle's entry). Blocked signal candles: open time up to
    last_processed_candle + N x candle length."""
    streak = int(loss_streak or 0)
    if trade_result is not None and trade_result < 0:
        streak += 1
    else:
        streak = 0
    cd = cooldown_cfg(plan)
    if cd is None or streak < int(cd.get("after_losses") or 1):
        return streak, None
    if last_processed_candle is None or not candle_ms:
        return streak, None
    return streak, int(last_processed_candle) + int(cd["candles"]) * int(candle_ms)


def cooldown_blocks(block_through, signal_candle_open) -> bool:
    return block_through is not None and signal_candle_open is not None and int(signal_candle_open) <= int(block_through)


def cooldown_candles_left(block_through, signal_candle_open, candle_ms) -> int:
    """How many candle closes are still skipped, counting the signal candle that is judged now."""
    if not cooldown_blocks(block_through, signal_candle_open) or not candle_ms:
        return 0
    return int((int(block_through) - int(signal_candle_open)) // int(candle_ms)) + 1


def loss_limit_cfg(plan: dict) -> dict | None:
    ll = (plan or {}).get("loss_limit")
    if isinstance(ll, dict) and ll.get("on") is True:
        return ll
    return None


# ==========================================================================
# CHART TYPE CANDLES
# ==========================================================================
def heikin_ashi(o, h, l, c):
    """TradingView Heikin Ashi (same as app.strategy.smoothed_candles)."""
    o, h, l, c = _to_np(o), _to_np(h), _to_np(l), _to_np(c)
    n = len(c)
    ha_c = (o + h + l + c) / 4.0
    ha_o = np.empty(n)
    for i in range(n):
        ha_o[i] = (o[i] + c[i]) / 2.0 if i == 0 else (ha_o[i - 1] + ha_c[i - 1]) / 2.0
    ha_h = np.maximum.reduce([h, ha_o, ha_c])
    ha_l = np.minimum.reduce([l, ha_o, ha_c])
    return ha_o, ha_h, ha_l, ha_c


def _true_range(h, l, c):
    """ta.tr(true): on the first bar (no previous close) = high - low."""
    prev = _shift(c)
    stacked = np.vstack([h - l, np.abs(h - prev), np.abs(l - prev)])
    out = np.max(np.where(np.isnan(stacked), -np.inf, stacked), axis=0)
    out[np.isnan(prev)] = (h - l)[np.isnan(prev)]
    return out


def _true_range_na(h, l, c):
    """The hub Pine's manual TR  math.max(h-l, abs(h-c[1]), abs(l-c[1]))  : na on the first bar."""
    out = _true_range(h, l, c)
    if len(out):
        out[0] = np.nan
    return out


def renko_candles(o, h, l, c, atr_length: int):
    """Renko built from the closed candles (close based), box size = ATR(atr_length) of the normal
    candles at each candle. Per candle the output is the LAST brick (open/high/low/close of that brick),
    carried forward on candles that make no new brick - the way TradingView's Renko ticker reads inside a
    strategy. Up brick: close >= last brick top + box. Down brick: close <= last brick bottom - box.
    APPROXIMATE vs TradingView (see module docstring)."""
    o, h, l, c = _to_np(o), _to_np(h), _to_np(l), _to_np(c)
    n = len(c)
    box_arr = pine_rma(_true_range(h, l, c), atr_length)
    ro = np.full(n, np.nan)
    rc = np.full(n, np.nan)
    top = bottom = np.nan      # last brick body
    have = False
    last_open = last_close = np.nan
    for i in range(n):
        box = box_arr[i]
        if not np.isfinite(box) or box <= 0:
            continue
        if not have:
            # first brick reference = this close; no brick yet
            top = bottom = c[i]
            last_open = last_close = c[i]
            have = True
            ro[i] = last_open
            rc[i] = last_close
            continue
        moved = True
        while moved:
            moved = False
            if c[i] >= top + box:          # new UP brick(s)
                last_open, last_close = top, top + box
                top, bottom = top + box, top
                moved = True
            elif c[i] <= bottom - box:     # new DOWN brick(s)
                last_open, last_close = bottom, bottom - box
                top, bottom = bottom, bottom - box
                moved = True
        ro[i] = last_open
        rc[i] = last_close
    rh = np.fmax(ro, rc)
    rl = np.fmin(ro, rc)
    return ro, rh, rl, rc


def chart_candles(df: pd.DataFrame, plan: dict):
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    ct = plan["chart_type"]
    if ct == "Candles":
        return _to_np(o), _to_np(h), _to_np(l), _to_np(c)
    if ct == "Heikin Ashi":
        return heikin_ashi(o, h, l, c)
    if ct == "Renko":
        return renko_candles(o, h, l, c, plan["renko_atr_length"])
    raise PlanError(f"unknown chart type {ct!r}")


# ==========================================================================
# INDICATOR HELPERS (Pine-exact)
# ==========================================================================
def pine_alma(src, length: int, offset: float, sigma: float) -> np.ndarray:
    """ta.alma. weight_i = exp(-(i-m)^2 / (2 s^2)), m = offset*(length-1), s = length/sigma; i = 0 is the
    OLDEST bar of the window, i = length-1 the newest. na if any value in the window is na."""
    s = _to_np(src)
    n = len(s)
    out = np.full(n, np.nan)
    m = offset * (length - 1)
    sg = length / sigma
    w = np.exp(-((np.arange(length) - m) ** 2) / (2.0 * sg * sg))
    norm = w.sum()
    for i in range(length - 1, n):
        win = s[i - length + 1:i + 1]
        if not np.isnan(win).any():
            out[i] = float(np.dot(win, w) / norm)
    return out


def pine_rsi(src, length: int) -> np.ndarray:
    s = _to_np(src)
    ch = s - _shift(s)
    up = np.where(np.isnan(ch), np.nan, np.maximum(ch, 0.0))
    dn = np.where(np.isnan(ch), np.nan, np.maximum(-ch, 0.0))
    ru = pine_rma(up, length)
    rd = pine_rma(dn, length)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ru / rd
        out = 100.0 - 100.0 / (1.0 + rs)
    out = np.where(rd == 0, 100.0, np.where(ru == 0, 0.0, out))   # Pine: down == 0 ? 100 : up == 0 ? 0 : ...
    out[np.isnan(ru) | np.isnan(rd)] = np.nan
    return out


def pine_macd(src, fast: int, slow: int, signal: int):
    s = _to_np(src)
    line = pine_ema(s, fast) - pine_ema(s, slow)
    sig = pine_ema(line, signal)
    return line, sig, line - sig


def rolling_highest(a, length: int) -> np.ndarray:
    a = _to_np(a)
    n = len(a)
    out = np.full(n, np.nan)
    for i in range(length - 1, n):
        w = a[i - length + 1:i + 1]
        if not np.isnan(w).any():
            out[i] = w.max()
    return out


def rolling_lowest(a, length: int) -> np.ndarray:
    a = _to_np(a)
    n = len(a)
    out = np.full(n, np.nan)
    for i in range(length - 1, n):
        w = a[i - length + 1:i + 1]
        if not np.isnan(w).any():
            out[i] = w.min()
    return out


def _cross_up(a, b) -> np.ndarray:
    a, b = _to_np(a), _to_np(b)
    pa, pb = _shift(a), _shift(b)
    with np.errstate(invalid="ignore"):
        return (a > b) & (pa <= pb)


def _cross_down(a, b) -> np.ndarray:
    a, b = _to_np(a), _to_np(b)
    pa, pb = _shift(a), _shift(b)
    with np.errstate(invalid="ignore"):
        return (a < b) & (pa >= pb)


def _pivots(x: np.ndarray, k: int, low: bool) -> np.ndarray:
    """Pine ta.pivotlow / ta.pivothigh with k candles left and right: True AT the pivot candle (it is only known k
    candles later). Low: lower than the k candles before it and not higher than the k after it (high: mirror)."""
    x = _to_np(x)
    m = len(x)
    out = np.zeros(m, dtype=bool)
    for p in range(k, m - k):
        v = x[p]
        if not math.isfinite(v):
            continue
        left, right = x[p - k:p], x[p + 1:p + 1 + k]
        if not (np.all(np.isfinite(left)) and np.all(np.isfinite(right))):
            continue
        if low and v < left.min() and v <= right.min():
            out[p] = True
        elif not low and v > left.max() and v >= right.max():
            out[p] = True
    return out


def rsi_divergence(low, high, rsi, k: int, lookback: int):
    """Regular RSI divergence, like TradingView's built-in RSI divergence: pivots are found on the RSI (k candles
    left / right). Bullish: this RSI pivot low is HIGHER than the previous one while the price low there is LOWER.
    Bearish: this RSI pivot high is LOWER than the previous one while the price high there is HIGHER. The two
    pivots must be k to `lookback` candles apart. The signal is on the candle the pivot is confirmed (k candles
    after it), never earlier. Returns (bull, bear) bool arrays."""
    low, high, rsi = _to_np(low), _to_np(high), _to_np(rsi)
    m = len(rsi)
    bull = np.zeros(m, dtype=bool)
    bear = np.zeros(m, dtype=bool)
    for arr, is_low, sig in ((rsi, True, bull), (rsi, False, bear)):
        piv = np.flatnonzero(_pivots(arr, k, is_low))
        for a, b in zip(piv[:-1], piv[1:]):
            if not (k <= b - a <= lookback) or b + k >= m:
                continue
            if is_low and rsi[b] > rsi[a] and low[b] < low[a]:
                sig[b + k] = True
            if not is_low and rsi[b] < rsi[a] and high[b] > high[a]:
                sig[b + k] = True
    return bull, bear


def pine_stdev(src, length: int) -> np.ndarray:
    """ta.stdev: the POPULATION standard deviation (divide by N) of the last `length` values; na until a full window."""
    s_ = _to_np(src)
    return pd.Series(s_).rolling(int(length)).std(ddof=0).to_numpy(dtype=float)


def pine_sar(h, l, c, start: float, inc: float, maximum: float) -> np.ndarray:
    """Parabolic SAR, the way Pine's ta.sar works (first value on the second candle; NaN before). The dots are below the
    price while the trend is up (SAR < close) and above it while it is down."""
    n = len(c)
    out = np.full(n, np.nan)
    if n < 2:
        return out
    result = maxmin = acc = float("nan")
    below = False
    for i in range(1, n):
        first = False
        if i == 1:
            if c[1] > c[0]:
                below, maxmin, result = True, h[1], l[0]
            else:
                below, maxmin, result = False, l[1], h[0]
            first, acc = True, start
        result = result + acc * (maxmin - result)
        if below:
            if result > l[i]:
                first, below = True, False
                result = max(h[i], maxmin)
                maxmin, acc = l[i], start
        else:
            if result < h[i]:
                first, below = True, True
                result = min(l[i], maxmin)
                maxmin, acc = h[i], start
        if not first:
            if below:
                if h[i] > maxmin:
                    maxmin, acc = h[i], min(acc + inc, maximum)
            else:
                if l[i] < maxmin:
                    maxmin, acc = l[i], min(acc + inc, maximum)
        if below:
            result = min(result, l[i - 1])
            if i > 1:
                result = min(result, l[i - 2])
        else:
            result = max(result, h[i - 1])
            if i > 1:
                result = max(result, h[i - 2])
        out[i] = result
    return out


def pine_supertrend(h, l, c, atr_length: int, factor: float):
    """Supertrend, the way Pine's ta.supertrend works (hl2 source), but on the CHART candles and with the hub's own ATR
    (manual true range, na on the first bar, RMA). Returns (line, up, valid): the Supertrend line, True where the trend
    is UP (line below the price) and where the ATR is known. Bars before the ATR is known have line = NaN."""
    h, l, c = _to_np(h), _to_np(l), _to_np(c)
    n = len(c)
    atr = pine_rma(_true_range_na(h, l, c), int(atr_length))
    src = (h + l) / 2.0
    ub0 = src + float(factor) * atr
    lb0 = src - float(factor) * atr
    fub = np.full(n, np.nan)
    flb = np.full(n, np.nan)
    line = np.full(n, np.nan)
    dirn = np.ones(n, dtype=int)          # Pine: 1 = down trend, -1 = up trend
    for i in range(n):
        plb = 0.0 if (i == 0 or np.isnan(flb[i - 1])) else flb[i - 1]       # nz(lowerBand[1])
        pub = 0.0 if (i == 0 or np.isnan(fub[i - 1])) else fub[i - 1]
        pc = c[i - 1] if i > 0 else np.nan
        lbi, ubi = lb0[i], ub0[i]
        flb[i] = lbi if (lbi > plb or pc < plb) else plb      # a NaN compares false, like Pine's na
        fub[i] = ubi if (ubi < pub or pc > pub) else pub
        if i == 0 or np.isnan(atr[i - 1]):
            d = 1
        else:
            prev_line = line[i - 1]
            if prev_line == pub:
                d = -1 if c[i] > fub[i] else 1
            else:
                d = 1 if c[i] < flb[i] else -1
        dirn[i] = d
        line[i] = flb[i] if d == -1 else fub[i]
    valid = ~np.isnan(atr)
    line = np.where(valid, line, np.nan)
    return line, (dirn == -1) & valid, valid


def _shift_bool(a: np.ndarray) -> np.ndarray:
    """The value one candle earlier; False on the first candle."""
    out = np.zeros(len(a), dtype=bool)
    if len(a) > 1:
        out[1:] = a[:-1]
    return out


def _within(cond: np.ndarray, n: int) -> np.ndarray:
    """nz(ta.barssince(cond), 9999) <= n"""
    out = np.zeros(len(cond), dtype=bool)
    since = 10 ** 9
    for i, v in enumerate(cond):
        since = 0 if v else since + 1
        out[i] = since <= n
    return out


def _ma(kind: str, src, length: int, offset: float, sigma: float) -> np.ndarray:
    if kind == "SMA":
        return pine_sma(src, length)
    if kind == "EMA":
        return pine_ema(src, length)
    if kind == "WMA":
        return pine_wma(src, length)
    if kind == "HMA":
        return pine_hma(src, length)
    if kind == "ALMA":
        return pine_alma(src, length, offset, sigma)
    raise PlanError(f"unknown MA type {kind!r}")


def _ma_cond(mode: str, src, ma, n: int, low=None, high=None):
    src, ma = _to_np(src), _to_np(ma)
    with np.errstate(invalid="ignore"):
        if mode == MA_PULLBACK:
            # the candle touches the MA and closes back on its side (long: low at / below the MA, close above)
            lo, hi = _to_np(low), _to_np(high)
            return _within((lo <= ma) & (src > ma), n), _within((hi >= ma) & (src < ma), n)
        if mode == "Price above / below MA":
            return src > ma, src < ma
        if mode == "MA rising / falling":
            prev = _shift(ma)
            return ma > prev, ma < prev
        if mode == "Price crossed MA within N candles":
            return _within(_cross_up(src, ma), n), _within(_cross_down(src, ma), n)
    raise PlanError(f"unknown MA condition {mode!r}")


# ==========================================================================
# THE HUB FRAME
# ==========================================================================
def compute_hub(df: pd.DataFrame, plan: dict) -> pd.DataFrame:
    """df: NORMAL closed candles, oldest -> newest, columns open_time/open/high/low/close/volume.
    Returns a copy with: the chart-type candle (c_open/c_high/c_low/c_close), every indicator value that was
    computed, <block>_long / <block>_short for every block, entry_long/entry_short/dir_e,
    flip_long/flip_short/dir_f, adx, tp_atr and the trailing-stop extremes."""
    plan = complete_plan(plan)
    out = df.copy().reset_index(drop=True)
    ind = plan["ind"]
    n = len(out)
    h, l, c, vol = _to_np(out["high"]), _to_np(out["low"]), _to_np(out["close"]), _to_np(out["volume"])

    cO, cH, cL, cC = chart_candles(out, plan)
    out["c_open"], out["c_high"], out["c_low"], out["c_close"] = cO, cH, cL, cC

    longs: dict[str, np.ndarray] = {}
    shorts: dict[str, np.ndarray] = {}
    with np.errstate(invalid="ignore"):
        longs["candle"], shorts["candle"] = cC > cO, cC < cO
        if ind["candle"].get("strong"):
            # strong candle only: an up candle with no lower wick, a down candle with no upper wick
            tol = 1e-9 * np.maximum(np.abs(cO), 1.0)
            longs["candle"] = longs["candle"] & (cL >= cO - tol)
            shorts["candle"] = shorts["candle"] & (cH <= cO + tol)
        if ind["candle"].get("prev_opposite"):
            # the candle before the signal candle was the OPPOSITE colour (long: it was a down candle, short: an up one)
            pO, pC = _shift(cO), _shift(cC)
            longs["candle"] = longs["candle"] & (pC < pO)
            shorts["candle"] = shorts["candle"] & (pC > pO)

    for name in ("ma1", "ma2"):
        p = ind[name]
        ma = _ma(p["type"], cC, p["length"], p["alma_offset"], p["alma_sigma"])
        out[name] = ma
        lg, sh = np.ones(n, dtype=bool), np.ones(n, dtype=bool)
        for m in p["modes"]:                   # every ticked condition must agree
            a, b_ = _ma_cond(m, cC, ma, p["n"], cL, cH)
            lg, sh = lg & a, sh & b_
        longs[name], shorts[name] = lg, sh

    p = ind["mx"]
    ma1, ma2 = out["ma1"].values, out["ma2"].values
    lg, sh = np.ones(n, dtype=bool), np.ones(n, dtype=bool)
    with np.errstate(invalid="ignore"):
        for m in p["modes"]:
            if m == "MA 1 above / below MA 2":
                a, b_ = ma1 > ma2, ma1 < ma2
            else:
                a, b_ = _within(_cross_up(ma1, ma2), p["n"]), _within(_cross_down(ma1, ma2), p["n"])
            lg, sh = lg & a, sh & b_
    longs["mx"], shorts["mx"] = lg, sh

    p = ind["adx"]
    di_plus, di_minus, adx = pine_dmi(out, p["di_length"], p["smoothing"])
    out["di_plus"], out["di_minus"], out["adx"] = di_plus, di_minus, adx
    with np.errstate(invalid="ignore"):
        ok = adx >= p["level"]
        if p.get("rising"):
            ok = ok & (adx > _shift(adx))       # ADX must be rising (the trend is getting stronger)
        longs["adx"] = ok & ((not p["di_required"]) | (di_plus > di_minus))
        shorts["adx"] = ok & ((not p["di_required"]) | (di_minus > di_plus))

    p = ind["ci"]
    tr_real = _true_range(h, l, c)          # ta.atr(1) = ta.tr(true)
    sum_tr = np.full(n, np.nan)
    L = p["length"]
    for i in range(L - 1, n):
        w = tr_real[i - L + 1:i + 1]
        if not np.isnan(w).any():
            sum_tr[i] = w.sum()
    with np.errstate(divide="ignore", invalid="ignore"):
        ci = 100.0 * np.log10(sum_tr / (rolling_highest(h, L) - rolling_lowest(l, L))) / math.log10(L)
    out["ci"] = ci
    with np.errstate(invalid="ignore"):
        ci_ok = ci < p["max"]
    longs["ci"], shorts["ci"] = ci_ok, ci_ok

    p = ind["rsi"]
    rsi = pine_rsi(cC, p["length"])
    out["rsi"] = rsi
    lg, sh = np.ones(n, dtype=bool), np.ones(n, dtype=bool)
    with np.errstate(invalid="ignore"):
        for mode in p["modes"]:             # every ticked condition must agree
            if mode == RSI_OBOS:
                # back inside: long when RSI crosses back UP through Oversold, short when it crosses back DOWN
                # through Overbought (the signal stays on for that candle and N more)
                up = _cross_up(rsi, np.full(n, float(p["oversold"])))
                dn = _cross_down(rsi, np.full(n, float(p["overbought"])))
                a, b_ = _within(up, int(p["n"])), _within(dn, int(p["n"]))
            elif mode == RSI_DIV:
                bull, bear = rsi_divergence(cL, cH, rsi, int(p["pivot"]), int(p["lookback"]))
                a, b_ = _within(bull, int(p["n"])), _within(bear, int(p["n"]))
            elif mode == RSI_MACROSS:
                # RSI against its OWN moving average (the average is taken of the RSI line, not of the price):
                # long when the RSI crosses UP through it, short when it crosses DOWN (stays on for N more candles)
                rsi_ma = _ma(p["ma_type"], rsi, int(p["ma_length"]), p["ma_alma_offset"], p["ma_alma_sigma"])
                out["rsi_ma"] = rsi_ma
                a, b_ = _within(_cross_up(rsi, rsi_ma), int(p["n"])), _within(_cross_down(rsi, rsi_ma), int(p["n"]))
            else:
                a, b_ = rsi > p["level"], rsi < p["level"]
            lg, sh = lg & a, sh & b_
    longs["rsi"], shorts["rsi"] = lg, sh

    p = ind["macd"]
    line, _sig, hist = pine_macd(cC, p["fast"], p["slow"], p["signal"])
    out["macd_hist"] = hist
    out["macd_line"] = line
    prev_hist = _shift(hist)
    lg, sh = np.ones(n, dtype=bool), np.ones(n, dtype=bool)
    with np.errstate(invalid="ignore"):
        for mode in p["modes"]:             # every ticked condition must agree
            src = line if mode == MACD_ZERO else hist
            lg, sh = lg & (src > 0), sh & (src < 0)
        longs["macd"] = lg & ((not p["rising"]) | (hist > prev_hist))
        shorts["macd"] = sh & ((not p["rising"]) | (hist < prev_hist))

    p = ind["vol"]
    with np.errstate(invalid="ignore"):
        avg = pine_sma(vol, p["length"])
        if p.get("exclude_current"):
            avg = _shift(avg)        # the average of the previous N candles, without the signal candle itself
        v_ok = vol >= avg * p["mult"]
    longs["vol"], shorts["vol"] = v_ok, v_ok

    p = ind["atr"]
    c_tr = _true_range_na(cH, cL, cC)
    c_atr = pine_rma(c_tr, p["length"])
    out["atr_chart"] = c_atr
    with np.errstate(invalid="ignore"):
        a_ok = c_atr >= pine_sma(c_atr, p["avg_length"]) * p["mult"]
        if p.get("rising"):
            a_ok = a_ok & (c_atr > _shift(c_atr))      # ATR must be rising (expanding)
    longs["atr"], shorts["atr"] = a_ok, a_ok

    p = ind["st"]
    hh = _shift(rolling_highest(cH, p["length"]))
    ll = _shift(rolling_lowest(cL, p["length"]))
    with np.errstate(invalid="ignore"):
        longs["st"], shorts["st"] = cC > hh, cC < ll

    p = ind["bb"]
    bb_basis = pine_sma(cC, p["length"])
    bb_dev = float(p["mult"]) * pine_stdev(cC, p["length"])
    bb_upper, bb_lower = bb_basis + bb_dev, bb_basis - bb_dev
    out["bb_basis"], out["bb_upper"], out["bb_lower"] = bb_basis, bb_upper, bb_lower
    with np.errstate(invalid="ignore"):
        longs["bb"] = _within(cL <= bb_lower, int(p["n"]))       # the low touched the bottom limit
        shorts["bb"] = _within(cH >= bb_upper, int(p["n"]))      # the high touched the top limit

    p = ind["eb"]
    eb_len = int(p["length"])
    eb_top, eb_mid, eb_bot = pine_ema(cH, eb_len), pine_ema(cC, eb_len), pine_ema(cL, eb_len)
    out["eb_top"], out["eb_mid"], out["eb_bot"] = eb_top, eb_mid, eb_bot
    eb_back = max(int(p["n"]) - 1, 0)           # N candles ending with the candle BEFORE the signal candle

    def _eb_up(line):         # the close is above the line and was below it within the last N candles
        with np.errstate(invalid="ignore"):
            was = _within(np.r_[False, (cC < line)[:-1]], eb_back)
            return (cC > line) & was

    def _eb_down(line):       # the close is below the line and was above it within the last N candles
        with np.errstate(invalid="ignore"):
            was = _within(np.r_[False, (cC > line)[:-1]], eb_back)
            return (cC < line) & was
    lg_eb, sh_eb = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    if p.get("long_low"):
        lg_eb = lg_eb | _eb_up(eb_bot)
    if p.get("long_high"):
        lg_eb = lg_eb | _eb_up(eb_top)
    if p.get("short_high"):
        sh_eb = sh_eb | _eb_down(eb_top)
    if p.get("short_low"):
        sh_eb = sh_eb | _eb_down(eb_bot)
    longs["eb"], shorts["eb"] = lg_eb, sh_eb

    p = ind["sar"]
    sar_v = pine_sar(cH, cL, cC, float(p["step"]), float(p["step"]), float(p["limit"]))
    out["sar_dots"] = sar_v
    with np.errstate(invalid="ignore"):
        longs["sar"], shorts["sar"] = cC > sar_v, cC < sar_v

    p = ind["sup"]
    sup_line, sup_up, sup_valid = pine_supertrend(cH, cL, cC, p["atr_length"], p["factor"])
    out["sup_line"] = sup_line
    out["sup_up"] = sup_up
    sup_down = sup_valid & ~sup_up
    prev_up, prev_down = _shift_bool(sup_up), _shift_bool(sup_down)
    lg, sh = np.ones(n, dtype=bool), np.ones(n, dtype=bool)
    for m in p["modes"]:                       # every ticked condition must agree
        if m == SUP_FLIP:
            a = _within(sup_up & prev_down, int(p["n"]))
            b_ = _within(sup_down & prev_up, int(p["n"]))
        else:
            a, b_ = sup_up, sup_down
        lg, sh = lg & a, sh & b_
    longs["sup"], shorts["sup"] = lg, sh

    # SUPPORT / RESISTANCE (2026-11-05): worked out on the NORMAL candles (see app/sr_engine.py). Off = all False.
    srp = sr_cfg(plan)
    if srp:
        sr_atr = pine_rma(_true_range_na(h, l, c), int(srp["atr_length"]))
        R = _sr.compute(_to_np(out["open"]), h, l, c, vol, sr_atr, srp)
    else:
        R = None
    zb, zf = np.zeros(n, dtype=bool), np.full(n, np.nan)
    longs["sr"], shorts["sr"] = (R["long"], R["short"]) if R else (zb, zb.copy())
    out["sr_origin"] = R["origin"].astype(int) if R else np.zeros(n, dtype=int)
    out["sr_stop_long"], out["sr_stop_short"] = (R["stop_long"], R["stop_short"]) if R else (zf, zf.copy())
    out["sr_xb_long"], out["sr_xb_short"] = (R["exit_bl"], R["exit_bs"]) if R else (zb.copy(), zb.copy())
    out["sr_xk_long"], out["sr_xk_short"] = (R["exit_kl"], R["exit_ks"]) if R else (zb.copy(), zb.copy())
    for key in ("s1", "s2", "r1", "r2", "mid", "lock"):
        out["sr_" + key] = R[key] if R else zf.copy()
    out["sr_lock_dir"] = R["lock_dir"].astype(int) if R else np.zeros(n, dtype=int)
    out["sr_break_up"], out["sr_break_dn"] = (R["break_up"], R["break_dn"]) if R else (zb.copy(), zb.copy())
    out["sr_conflict"] = R["conflict"] if R else zb.copy()
    out.attrs["sr_levels"] = R["levels_now"] if R else []

    for b in BLOCKS:
        out[f"{b}_long"] = longs[b]
        out[f"{b}_short"] = shorts[b]

    def combine(which: str, mode: str):
        used = [b for b in BLOCKS if ind[b][which]]
        cnt = len(used)
        nl = np.zeros(n, dtype=int)
        ns = np.zeros(n, dtype=int)
        for b in used:
            nl += longs[b].astype(int)
            ns += shorts[b].astype(int)
        if cnt == 0:
            z = np.zeros(n, dtype=bool)
            return z, z.copy(), np.zeros(n, dtype=int)
        if mode == COMBINE_ALL:
            lg, sh = nl == cnt, ns == cnt
        elif mode == COMBINE_ATLEAST:
            need_n = int(plan["entry_min"] if which == "entry" else plan["flip_min"])
            lg, sh = nl >= need_n, ns >= need_n
        else:
            lg, sh = nl > 0, ns > 0
        main = plan.get("main_entry" if which == "entry" else "main_flip")
        if main:                                     # the Main has to happen; the others are the confluence
            lg, sh = lg & longs[main], sh & shorts[main]
        d = np.where(lg & ~sh, 1, np.where(sh & ~lg, -1, 0))
        return lg, sh, d

    out["entry_long"], out["entry_short"], out["dir_e"] = combine("entry", plan["combine_entry"])
    if srp and srp.get("alone"):
        # Support / Resistance as its own strategy: its entries are added to the combined entry signal
        el, es = out["entry_long"].values | longs["sr"], out["entry_short"].values | shorts["sr"]
        out["entry_long"], out["entry_short"] = el, es
        out["dir_e"] = np.where(el & ~es, 1, np.where(es & ~el, -1, 0))
    if flip_same_entry(plan):     # Flip: Same as entry = Yes -> the flip uses the ENTRY signal itself
        out["flip_long"], out["flip_short"], out["dir_f"] = out["entry_long"], out["entry_short"], out["dir_e"]
    else:
        out["flip_long"], out["flip_short"], out["dir_f"] = combine("flip", plan["combine_flip"])

    # base signals for the first-reversal rule = the entry signal itself
    out["size_ema"] = pine_ema(cC, plan["sizing"]["counter"]["ema_length"])   # chart close, for trend / counter sizing
    by = plan["sizing"]["counter"].get("by", CT_EMA)
    if by == "bb":          # Bollinger half: LONG with the trend in the lower half of the band, SHORT in the upper half
        with np.errstate(invalid="ignore"):
            out["ct_long"], out["ct_short"] = cC < bb_basis, cC > bb_basis
    elif by in CT_BLOCKS:   # counter trend decided by an indicator: the side it says on this candle
        out["ct_long"], out["ct_short"] = longs[by], shorts[by]
    else:
        out["ct_long"], out["ct_short"] = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    # EXIT WHEN: the MA value after the candle close is against the trade (long: lower than the one before, short: higher)
    ew = exit_cfg(plan)
    ex_long, ex_short = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    if ew:
        with np.errstate(invalid="ignore"):
            m1, m2 = out["ma1"].values, out["ma2"].values
            f1, r1, f2, r2 = m1 < _shift(m1), m1 > _shift(m1), m2 < _shift(m2), m2 > _shift(m2)
        if ew.get("signal") == EXIT_MA2:
            ex_long, ex_short = f2, r2
        else:
            ex_long, ex_short = f1 & f2, r1 & r2
    out["exit_long"], out["exit_short"] = ex_long, ex_short
    tp = plan["tp"]
    tr_n = _true_range_na(h, l, c)
    out["tp_atr"] = pine_rma(tr_n, tp["atr_length"])
    # ATR of the NORMAL candles for the Stop Loss and the Step Trailing Stop (fixed at the entry candle).
    out["sl_atr"] = pine_rma(tr_n, plan["sl"]["atr_length"])
    out["be_atr"] = pine_rma(tr_n, int((plan.get("be") or {}).get("atr_length") or 14))   # Break-Even After ATR
    out["ts_atr"] = pine_rma(tr_n, plan["trail"]["atr_length"])
    tl = plan["trail"]["length"]
    out["trail_low"] = rolling_lowest(l, tl)
    out["trail_high"] = rolling_highest(h, tl)
    sll = int(plan["sl"].get("length") or 10)          # Stop Loss Structure / Chandelier: last N NORMAL candles
    out["sl_low"] = rolling_lowest(l, sll)
    out["sl_high"] = rolling_highest(h, sll)
    if sl_swing(plan):
        out["sl_piv_low"], out["sl_piv_high"] = _sl_pivots(h, l, c, int(plan["sl"]["struct_n"]))
    return out


# ==========================================================================
# SNAPSHOT OF THE LATEST CLOSED CANDLE
# ==========================================================================
@dataclass
class HubSnapshot:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    dir_e: int
    dir_f: int
    adx: float
    tp_atr: float
    trail_low: float
    trail_high: float
    c_close: float
    ma1: float
    ma2: float
    size_ema: float = float("nan")
    sl_atr: float = float("nan")
    ts_atr: float = float("nan")
    be_atr: float = float("nan")     # Break-Even After ATR: the ATR at the entry candle
    sl_low: float = float("nan")     # Stop Loss Structure / Chandelier: lowest low / highest high of its N candles
    sl_high: float = float("nan")
    sl_piv_low: float = float("nan")     # Stop Loss Structure "Real swing point": latest confirmed swing low below the close
    sl_piv_high: float = float("nan")    # ... latest confirmed swing high above the close
    ct_long: bool = False        # counter trend decided by an indicator: it says LONG / SHORT on this candle
    ct_short: bool = False
    sup_line: float = float("nan")   # Trailing Stop "Supertrend line": the line of this candle
    exit_long: bool = False      # Exit when: the MA(s) curve against a LONG / a SHORT on this candle close
    exit_short: bool = False
    sr_origin: int = 0           # Support / Resistance: 0 none, 1 bounce, 2 breakout, 3 retest (the entry signal of this candle)
    sr_stop_long: float = float("nan")    # the stop outside the zone for a LONG / SHORT entered on this candle
    sr_stop_short: float = float("nan")
    sr_xb_long: bool = False     # SR exit (bounce ticks) against a LONG / a SHORT on this candle close
    sr_xb_short: bool = False
    sr_xk_long: bool = False     # SR exit (breakout ticks: the close is back inside the range)
    sr_xk_short: bool = False
    sr_long: bool = False
    sr_short: bool = False

    def to_display_dict(self) -> dict:
        def side(d):
            return "LONG" if d == 1 else "SHORT" if d == -1 else "NONE"
        return {
            "candle_open_time": self.open_time, "close": self.close, "high": self.high, "low": self.low,
            "chart_close": self.c_close, "ma1": self.ma1, "ma2": self.ma2, "adx": self.adx,
            "entry_signal": side(self.dir_e), "flip_signal": side(self.dir_f), "signal": side(self.dir_e),
            "tp_atr": self.tp_atr,
        }


def _f(x) -> float:
    return float(x) if x is not None else float("nan")


def hub_snapshot_at(frame: pd.DataFrame, i: int) -> HubSnapshot:
    r = frame.iloc[i]
    return HubSnapshot(
        open_time=int(r["open_time"]), open=_f(r["open"]), high=_f(r["high"]), low=_f(r["low"]),
        close=_f(r["close"]), dir_e=int(r["dir_e"]), dir_f=int(r["dir_f"]), adx=_f(r["adx"]),
        tp_atr=_f(r["tp_atr"]), trail_low=_f(r["trail_low"]), trail_high=_f(r["trail_high"]),
        c_close=_f(r["c_close"]), ma1=_f(r["ma1"]), ma2=_f(r["ma2"]),
        size_ema=_f(r["size_ema"]) if "size_ema" in r.index else float("nan"),
        sl_atr=_f(r["sl_atr"]) if "sl_atr" in r.index else float("nan"),
        ts_atr=_f(r["ts_atr"]) if "ts_atr" in r.index else float("nan"),
        be_atr=_f(r["be_atr"]) if "be_atr" in r.index else float("nan"),
        sl_low=_f(r["sl_low"]) if "sl_low" in r.index else float("nan"),
        sl_high=_f(r["sl_high"]) if "sl_high" in r.index else float("nan"),
        sl_piv_low=_f(r["sl_piv_low"]) if "sl_piv_low" in r.index else float("nan"),
        sl_piv_high=_f(r["sl_piv_high"]) if "sl_piv_high" in r.index else float("nan"),
        ct_long=bool(r["ct_long"]) if "ct_long" in r.index else False,
        ct_short=bool(r["ct_short"]) if "ct_short" in r.index else False,
        sup_line=_f(r["sup_line"]) if "sup_line" in r.index else float("nan"),
        exit_long=bool(r["exit_long"]) if "exit_long" in r.index else False,
        exit_short=bool(r["exit_short"]) if "exit_short" in r.index else False,
        sr_origin=int(r["sr_origin"]) if "sr_origin" in r.index else 0,
        sr_stop_long=_f(r["sr_stop_long"]) if "sr_stop_long" in r.index else float("nan"),
        sr_stop_short=_f(r["sr_stop_short"]) if "sr_stop_short" in r.index else float("nan"),
        sr_xb_long=bool(r["sr_xb_long"]) if "sr_xb_long" in r.index else False,
        sr_xb_short=bool(r["sr_xb_short"]) if "sr_xb_short" in r.index else False,
        sr_xk_long=bool(r["sr_xk_long"]) if "sr_xk_long" in r.index else False,
        sr_xk_short=bool(r["sr_xk_short"]) if "sr_xk_short" in r.index else False,
        sr_long=bool(r["sr_long"]) if "sr_long" in r.index else False,
        sr_short=bool(r["sr_short"]) if "sr_short" in r.index else False,
    )


def last_hub_snapshot(frame: pd.DataFrame) -> HubSnapshot:
    return hub_snapshot_at(frame, len(frame) - 1)


# ==========================================================================
# DECISIONS (same order as the hub Pine)
# ==========================================================================
def flip_decision(direction: str, snap: HubSnapshot, entry_price: float, plan: dict) -> str:
    """In a position: "FLIP" (the opposite flip signal, not held), "HOLD" (held by the Hold Rule) or
    "STAY". LONG flips on dir_f == -1, SHORT flips on dir_f == +1.
    Hold Rule: plan.hold.on and open profit > 0 and ADX < level (NaN ADX -> not held)."""
    opposite = -1 if direction == "LONG" else 1
    if snap.dir_f != opposite:
        return "STAY"
    h = effective_hold(plan)
    if h["on"]:
        profit = (snap.close - entry_price) if direction == "LONG" else (entry_price - snap.close)
        if profit > 0 and snap.adx < h["adx_level"]:
            return "HOLD"
    return "FLIP"


def exit_decision(direction: str, snap: HubSnapshot, plan: dict) -> str:
    """In a position: "EXIT" when Exit when is ON and its signal is against the open trade on this candle close, else
    "STAY". (Whether it then waits or flips is exit_after.)"""
    if not exit_cfg(plan):
        return "STAY"
    hit = bool(getattr(snap, "exit_long", False)) if direction == "LONG" else bool(getattr(snap, "exit_short", False))
    return "EXIT" if hit else "STAY"


def sr_exit_decision(direction: str, snap: HubSnapshot, plan: dict, origin: int) -> str:
    """Support / Resistance exits, judged at a candle close: "EXIT" or "STAY". Which exits count depends on where
    they apply: "every trade" = any ticked exit closes any trade; "SR trades only" = a trade that a bounce entered
    uses the bounce exits, one that a breakout or a retest entered uses the breakout exit (the close back inside)."""
    sr = sr_cfg(plan)
    if not sr_exit_ticked(sr):
        return "STAY"
    long_ = direction == "LONG"
    xb = bool(getattr(snap, "sr_xb_long" if long_ else "sr_xb_short", False))
    xk = bool(getattr(snap, "sr_xk_long" if long_ else "sr_xk_short", False))
    if sr.get("exit_all"):
        hit = xb or xk
    elif sr.get("exit_sr_only"):
        hit = xb if origin == _sr.ORIGIN_BOUNCE else xk if origin in (_sr.ORIGIN_BREAKOUT, _sr.ORIGIN_RETEST) else False
    else:
        hit = False
    return "EXIT" if hit else "STAY"


def exit_after(plan: dict) -> str:
    ew = exit_cfg(plan)
    return ew.get("after", EXIT_WAIT) if ew else EXIT_WAIT


def sup_trail_level(direction: str, line, price) -> float | None:
    """Trailing Stop "Supertrend line": the line, if it is on the protective side of the price (long: below it, short:
    above it). A line on the wrong side (the Supertrend points the other way) gives nothing."""
    if line is None or price is None or not math.isfinite(line) or not math.isfinite(price):
        return None
    ok = line < price if direction == "LONG" else line > price
    return float(line) if ok else None


def entry_size(direction: str, snap: HubSnapshot, plan: dict) -> tuple[float, float, str]:
    """(allocation %, leverage, kind) of an entry. kind is "single" (counter-trend sizing is OFF), "trend" or
    "counter". With counter-trend sizing ON: LONG is with the trend when the chart close is ABOVE the EMA, SHORT
    when BELOW; anything else (including an EMA that is not computed) is counter-trend (the smaller, safer size)."""
    sz = plan["sizing"]
    ct = sz.get("counter") or {"on": False}
    if not ct.get("on"):
        return float(sz["alloc_pct"]), float(sz["leverage"]), "single"
    by = ct.get("by", CT_EMA)
    if by in CT_BLOCKS:
        # decided by an indicator: with the trend when it says the same side as the entry (its own conditions)
        with_trend = bool(getattr(snap, "ct_long", False)) if direction == "LONG" else bool(getattr(snap, "ct_short", False))
    else:
        ema = snap.size_ema
        if direction == "LONG":
            with_trend = bool(math.isfinite(ema) and snap.c_close > ema)
        else:
            with_trend = bool(math.isfinite(ema) and snap.c_close < ema)
    if with_trend:
        return float(sz["alloc_pct"]), float(sz["leverage"]), "trend"
    return float(ct["alloc_pct"]), float(ct["leverage"]), "counter"


def stop_price_from_loss(direction: str, entry_price: float, equity: float, qty: float, stop_pct: float) -> float:
    """Max loss per trade: stop = entry -/+ (equity * stop% / qty)."""
    if qty <= 0:
        raise ValueError("qty must be > 0")
    dist = equity * (stop_pct / 100.0) / abs(qty)
    return entry_price - dist if direction == "LONG" else entry_price + dist


def counter_entry_off(kind: str, plan: dict) -> bool:
    """Counter-trend size keyed in as 0 (allocation % or base amount) = no counter-trend entry at all
    (owner 2026-10-25). A flip into that side closes and stays flat."""
    if kind != "counter":
        return False
    ct = plan["sizing"].get("counter") or {}
    key = "base_amount" if uses_fixed_base(plan) else "alloc_pct"
    try:
        return float(ct.get(key) or 0.0) <= 0.0
    except (TypeError, ValueError):
        return False


def uses_fixed_base(plan: dict) -> bool:
    """True when ENTRY SIZE is the fixed base amount that grows with the last closed total."""
    return plan["sizing"].get("method", SIZE_ALLOC) == SIZE_FIXED


def fixed_entry_amount(kind: str, plan: dict, last_total) -> float:
    """Fixed base method: the margin (USDT) of the next entry.
    base  = the base amount of this entry type (kind "counter" = the counter-trend base, else the trend base)
    next  = the total of the last fully closed trade (its entry amount + all its fills), but never below base.
    No last closed trade yet (last_total is None / not a number) = the base."""
    sz = plan["sizing"]
    base = float(plan["sizing"]["counter"]["base_amount"]) if kind == "counter" else float(sz["base_amount"])
    if last_total is None or not math.isfinite(last_total):
        return base
    return max(base, float(last_total))


def carry_total(amount, pnl, lock_pct) -> float:
    """What the next entry is built on after a fully closed trade.
    Lock-out OFF (lock_pct None): the whole total = entry amount + all fills.
    Lock-out ON: a profit adds only lock_pct % of it to the entry amount (the rest is kept); a loss comes off in full."""
    amount = float(amount)
    pnl = float(pnl or 0.0)
    if lock_pct is None or pnl <= 0:
        return amount + pnl
    return amount + pnl * float(lock_pct) / 100.0


def max_loss_usd(plan: dict, available_balance: float, margin: float) -> float:
    """The most one trade may lose, in USDT, by the MAX LOSS basis of the plan:
       % of available balance  -> available_balance * pct
       % of entry amount       -> margin (the entry amount, before leverage) * pct
       Fixed $ amount          -> that amount"""
    sz = plan["sizing"]
    basis = sz.get("stop_basis", STOP_BAL)
    if basis == STOP_USD:
        return float(sz["stop_usd"])
    base = margin if basis == STOP_ENTRY else available_balance
    return base * (float(sz["stop_pct"]) / 100.0)


def stop_price_from_usd(direction: str, entry_price: float, qty: float, loss_usd: float) -> float:
    """Stop price at which the position loses loss_usd: entry -/+ (loss_usd / qty)."""
    if qty <= 0:
        raise ValueError("qty must be > 0")
    dist = loss_usd / abs(qty)
    return entry_price - dist if direction == "LONG" else entry_price + dist


def paper_size(direction: str, snap: "HubSnapshot", plan: dict, last_total, balance):
    """Size of a PAPER (shadow) entry: (allocation % of wallet, leverage, margin USDT or None), or None when it
    cannot be worked out because the balance is needed and not known.
    Allocation method: the plan's allocation %. Fixed base method: the amount the next real entry would use,
    turned into a % of the available balance (the paper tracker counts in % of the wallet)."""
    alloc, lev, kind = entry_size(direction, snap, plan)
    if counter_entry_off(kind, plan):
        return None
    if uses_fixed_base(plan):
        if balance is None or not math.isfinite(balance) or balance <= 0:
            return None
        margin = fixed_entry_amount(kind, plan, last_total)
        return margin / balance * 100.0, lev, margin
    margin = (balance * alloc / 100.0) if (balance is not None and math.isfinite(balance) and balance > 0) else None
    return alloc, lev, margin


def paper_stop_fraction(plan: dict, alloc: float, lev: float, margin):
    """How far from the entry price a PAPER stop sits, as a fraction of the price (None = cannot be worked out)."""
    sz = plan["sizing"]
    basis = sz.get("stop_basis", STOP_BAL)
    if basis == STOP_BAL:
        return (sz["stop_pct"] / 100.0) / (alloc / 100.0 * lev)
    if basis == STOP_ENTRY:
        return (sz["stop_pct"] / 100.0) / lev
    if margin is None or margin <= 0:
        return None
    return float(sz["stop_usd"]) / (margin * lev)


def tp_from(plan: dict) -> str:
    """Where the take-profit targets are counted from. A plan without the key (saved before 2026-10-27) = Entry price."""
    return (plan.get("tp") or {}).get("from") or TP_FROM_ENTRY


def tp_base_price(plan: dict, entry_price: float, signal_close) -> float:
    """The price the 3 targets are counted from: the signal candle close when the plan says so (and it is a good
    number), otherwise the entry price."""
    if tp_from(plan) == TP_FROM_SIGNAL:
        try:
            sc = float(signal_close)
        except (TypeError, ValueError):
            sc = float("nan")
        if math.isfinite(sc) and sc > 0:
            return sc
    return entry_price


def tp_prices(direction: str, entry_price: float, atr: float, plan: dict) -> list[dict]:
    """The 3 TP levels of the plan (only if take profit is ON). Each: {index, price, pct, on}. 'on' False
    levels are returned too so the caller keeps the numbering."""
    out = []
    tp = upgrade_plan(plan)["tp"]
    for k, lv in enumerate(tp["levels"], 1):
        dist = distance(tp.get("basis", BASIS_ATR), lv["value"], entry_price, atr)
        if dist is None:
            dist = float("nan")
        price = entry_price + dist if direction == "LONG" else entry_price - dist
        out.append({"index": k, "price": price, "pct": lv["pct"], "on": bool(tp["on"] and lv["on"])})
    return out


# --------------------------------------------------------------------------
# STOP LADDER (owner design 2026-10-23): Max Loss, Stop Loss, Trailing Stop. The NEAREST level rests on the
# exchange; the levels behind it are watched by the bot on the live price. Pure helpers only.
# --------------------------------------------------------------------------
LADDER_ORDER = ("ts", "sl", "ml")      # on a tie the Trailing Stop, then the Stop Loss, goes on the exchange
LADDER_NAMES = {"ml": "Max Loss", "sl": "Stop Loss", "ts": "Trailing Stop"}


def distance(basis: str, value, entry_price: float, atr) -> float | None:
    """A distance in price from the entry. "% of ATR": value/100 x ATR. "% of price movement": value/100 x entry
    price. None when it cannot be worked out (no ATR yet, bad number)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    if basis == BASIS_PRICE:
        return entry_price * v / 100.0 if entry_price and entry_price > 0 else None
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return None
    return atr * v / 100.0


def signed_distance(basis: str, value, entry_price: float, atr) -> float | None:
    """Like distance(), but 0 and minus are allowed (Trailing Stop "Stop goes to": 0 = breakeven, plus = profit
    locked in, minus = below the entry for a long)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if v == 0:
        return 0.0
    d = distance(basis, abs(v), entry_price, atr)
    return None if d is None else (d if v > 0 else -d)


def level_from_entry(direction: str, entry_price: float, dist: float) -> float:
    """A protective level: below the entry for a long, above it for a short."""
    return entry_price - dist if direction == "LONG" else entry_price + dist


def sl_level(direction: str, entry_price: float, atr, plan: dict, snap=None, qty=None) -> float | None:
    """The Stop Loss price of a new trade, worked out once at entry. None = it cannot be worked out (ATR not ready,
    no candles / qty for that method, or the result is on the wrong side of the entry price)."""
    srs = _sr_stop_level(direction, entry_price, plan, snap)
    sl = plan.get("sl") or {}
    if not sl.get("on"):
        return srs
    plain = _plain_sl_level(direction, entry_price, atr, plan, snap, qty)
    if plain is None or srs is None:
        return plain if srs is None else srs
    return srs if is_tighter(direction, srs, plain) else plain      # both are active: the one the price meets first


def _sr_stop_level(direction: str, entry_price: float, plan: dict, snap) -> float | None:
    """The Support / Resistance stop (outside the zone) of a trade entered on the Support / Resistance candle."""
    if snap is None or not sr_stop_on(plan) or not getattr(snap, "sr_origin", 0):
        return None
    lvl = getattr(snap, "sr_stop_long" if direction == "LONG" else "sr_stop_short", None)
    if lvl is None or not math.isfinite(lvl) or lvl <= 0:
        return None
    if (direction == "LONG" and lvl >= entry_price) or (direction == "SHORT" and lvl <= entry_price):
        return None
    return float(lvl)


def _plain_sl_level(direction: str, entry_price: float, atr, plan: dict, snap=None, qty=None) -> float | None:
    sl = plan.get("sl") or {}
    b = sl.get("basis")
    lvl = None
    if b in (BASIS_ATR, BASIS_PRICE):
        d = distance(b, sl.get("value"), entry_price, atr)
        lvl = None if d is None else level_from_entry(direction, entry_price, d)
    elif b == SL_USD:
        try:
            if qty and float(qty) > 0:
                lvl = stop_price_from_usd(direction, entry_price, float(qty), float(sl.get("usd")))
        except (TypeError, ValueError):
            lvl = None
    elif b == SL_STRUCT and snap is not None:
        if sl.get("struct_mode") == SLS_SWING:
            lvl = getattr(snap, "sl_piv_low" if direction == "LONG" else "sl_piv_high", None)
        else:
            lvl = getattr(snap, "sl_low" if direction == "LONG" else "sl_high", None)
    elif b == SL_CHAND and snap is not None:
        d = distance(BASIS_ATR, sl.get("value"), entry_price, atr)
        ext = getattr(snap, "sl_high" if direction == "LONG" else "sl_low", None)
        if d is not None and ext is not None and math.isfinite(ext):
            lvl = ext - d if direction == "LONG" else ext + d
    elif b == SL_MA and snap is not None:
        lvl = getattr(snap, "ma2" if sl.get("ma") == "MA 2" else "ma1", None)
    if lvl is None or not math.isfinite(lvl) or lvl <= 0:
        return None
    if (direction == "LONG" and lvl >= entry_price) or (direction == "SHORT" and lvl <= entry_price):
        return None                     # on the wrong side of the entry: it cannot be used as a stop
    return float(lvl)


def be_setup(direction: str, entry_price: float, atr, plan: dict) -> dict | None:
    """BREAK-EVEN of a new trade (owner 2026-10-26), fixed at entry: {"tp": n or None, "trigger": price or None,
    "done": False}. None = Break-Even is off. After ATR: trigger = entry +/- value% x ATR (ATR at the entry candle;
    None when the ATR is not ready)."""
    be = plan.get("be") or {}
    if not be.get("on"):
        return None
    trig = None
    if be.get("after_atr"):
        d = distance(BASIS_ATR, be.get("atr_value"), entry_price, atr)
        trig = None if d is None else level_from_entry(direction, entry_price, -d)
    return {"tp": int(be["tp_n"]) if be.get("after_tp") else None, "trigger": trig, "done": False}


def be_due(direction: str, be: dict | None, price: float, tp_filled: set) -> bool:
    """Break-Even is reached: TP n filled, or the price touched the After ATR trigger (whichever comes first)."""
    if not be or be.get("done"):
        return False
    if be.get("tp") and be["tp"] in tp_filled:
        return True
    t = be.get("trigger")
    if t is not None and price is not None:
        return price >= t if direction == "LONG" else price <= t
    return False


def time_stop_due(direction: str, plan: dict, candles_open: int, close: float, entry_price: float) -> bool:
    """TIME STOP (owner 2026-10-26): the trade has been open N candles and is not in profit -> close it."""
    sl = plan.get("sl") or {}
    if not (sl.get("on") and sl.get("time_on")):
        return False
    if candles_open < int(sl.get("time_candles") or 0):
        return False
    in_profit = close > entry_price if direction == "LONG" else close < entry_price
    return not in_profit


def step_setup(direction: str, entry_price: float, atr, plan: dict, sl_price: float | None) -> dict | None:
    """Step Trailing Stop: its start level and its gap (in price), or None if it is not used / cannot be worked out."""
    tr = plan.get("trail") or {}
    if not (tr.get("on") and tr.get("type") == TRAIL_STEP):
        return None
    gap = distance(tr.get("basis"), tr.get("gap"), entry_price, atr)
    if gap is None or gap <= 0:
        return None
    if tr.get("start") == START_BEGIN:
        # Begins when the price moves A in favour -> the stop goes to entry + L -> then moves every gap.
        begin = distance(tr.get("basis"), tr.get("begin_value"), entry_price, atr)
        lock = signed_distance(tr.get("basis"), tr.get("lock_value"), entry_price, atr)
        if begin is None or lock is None or lock >= begin:
            return None
        first = entry_price + lock if direction == "LONG" else entry_price - lock
        return {"start": float(first - gap if direction == "LONG" else first + gap), "gap": float(gap),
                "begin": float(begin), "first": float(first)}
    if tr.get("start") == START_SL:
        start = sl_price
    else:
        d = distance(tr.get("basis"), tr.get("start_value"), entry_price, atr)
        start = None if d is None else level_from_entry(direction, entry_price, d)
    if start is None or not math.isfinite(start):
        return None
    return {"start": float(start), "gap": float(gap)}


def step_level(direction: str, entry_price: float, start: float, gap: float, best: float,
               begin: float | None = None, first: float | None = None) -> float | None:
    """Each time the price has moved one more gap in favour (counted from the entry), the stop moves one gap from
    its start. None before the first checkpoint (the Trailing Stop is not in charge yet).
    Example (long, entry 100, start 98, gap 0.5): best 100.5 -> 98.5, best 101 -> 99, best 102 -> 100.
    "Begins at a price move" (begin / first given): nothing before the price moved `begin`; then the stop is at
    `first`, and it moves one gap for every further gap.
    Example (long, entry 100, begin 5, first 100 = breakeven, gap 2.5): 105 -> 100, 107.5 -> 102.5, 110 -> 105."""
    if gap <= 0 or best is None or not math.isfinite(best):
        return None
    move = (best - entry_price) if direction == "LONG" else (entry_price - best)
    if begin is not None and first is not None:
        if move < begin - 1e-9 * max(1.0, abs(begin)):
            return None
        k = math.floor((move - begin) / gap + 1e-9)
        return first + k * gap if direction == "LONG" else first - k * gap
    k = math.floor(move / gap + 1e-9)
    if k < 1:
        return None
    return start + k * gap if direction == "LONG" else start - k * gap


def candle_trail_distance(plan: dict, entry_price: float, atr) -> float | None:
    """Signal candle +/- ATR Trailing Stop: the distance in price (value % of the ATR of the signal candle, fixed for
    the trade). None when the ATR is not available yet."""
    tr = plan.get("trail") or {}
    if not (tr.get("on") and tr.get("type") == TRAIL_CANDLE):
        return None
    return distance(BASIS_ATR, tr.get("value"), entry_price, atr)


def candle_start_profit(plan: dict) -> bool:
    """The Signal candle trail waits until the trade is in profit by candle_profit (% of the signal candle ATR)."""
    tr = plan.get("trail") or {}
    return bool(tr.get("on") and tr.get("type") == TRAIL_CANDLE and tr.get("candle_start") == TCS_PROFIT)


def candle_arm_price(direction: str, entry_price: float, atr, plan: dict) -> float | None:
    """The price a candle must CLOSE at (or beyond) before the trail may take charge: entry +/- candle_profit % of
    the signal candle ATR. None when it is not used or cannot be worked out (ATR not ready)."""
    if not candle_start_profit(plan):
        return None
    d = distance(BASIS_ATR, (plan.get("trail") or {}).get("candle_profit"), entry_price, atr)
    if d is None or not math.isfinite(d) or d <= 0:
        return None
    return float(entry_price + d if direction == "LONG" else entry_price - d)


def candle_armed(direction: str, close, arm_price) -> bool:
    """A candle closed at or beyond the arming price (long: at or above, short: at or below)."""
    if arm_price is None or close is None or not math.isfinite(close):
        return False
    return close >= arm_price if direction == "LONG" else close <= arm_price


def sl_swing(plan: dict) -> bool:
    """Stop Loss Structure found by a real swing point (N candles each side)."""
    sl = plan.get("sl") or {}
    return bool(sl.get("on") and sl.get("basis") == SL_STRUCT and sl.get("struct_mode") == SLS_SWING)


def _sl_pivots(h, l, c, n_side: int):
    """Per candle: the latest swing low (below this close) and swing high (above this close) that was already
    CONFIRMED on this candle (a swing is known n_side candles after it formed, so nothing looks ahead)."""
    n = len(c)
    lo_out = np.full(n, np.nan)
    hi_out = np.full(n, np.nan)
    hi, lo = _sr.swing_points(np.asarray(h, dtype=float), np.asarray(l, dtype=float), n_side)
    kh = kl = 0
    for i in range(n):
        while kh < len(hi) and hi[kh][0] + n_side <= i:
            kh += 1
        while kl < len(lo) and lo[kl][0] + n_side <= i:
            kl += 1
        if not math.isfinite(c[i]):
            continue
        for j in range(kl - 1, max(kl - 501, -1), -1):
            if lo[j][1] < c[i]:
                lo_out[i] = lo[j][1]
                break
        for j in range(kh - 1, max(kh - 501, -1), -1):
            if hi[j][1] > c[i]:
                hi_out[i] = hi[j][1]
                break
    return lo_out, hi_out


def candle_trail_level(direction: str, low, high, dist) -> float | None:
    """Long: the candle low minus the distance. Short: the candle high plus the distance."""
    if dist is None or not math.isfinite(dist) or dist <= 0:
        return None
    ext = low if direction == "LONG" else high
    if ext is None or not math.isfinite(ext):
        return None
    return float(ext - dist if direction == "LONG" else ext + dist)


def is_tighter(direction: str, a: float, b: float | None) -> bool:
    """a protects more than b (long: higher, short: lower). Anything is tighter than no level."""
    if b is None:
        return True
    return a > b if direction == "LONG" else a < b


def nearest_level(direction: str, levels: dict) -> tuple[str | None, float | None]:
    """The level that goes on the exchange: the one nearest to the price on the protective side."""
    best_name, best_price = None, None
    for name in LADDER_ORDER:
        p = levels.get(name)
        if p is None or not math.isfinite(p):
            continue
        if best_price is None or is_tighter(direction, p, best_price):
            best_name, best_price = name, p
    return best_name, best_price


def passed(direction: str, level: float, price: float) -> bool:
    """The live price has gone THROUGH the level (long: below it, short: above it)."""
    return price < level if direction == "LONG" else price > level


def trail_stop(direction: str, current_stop: float | None, snap: HubSnapshot) -> float | None:
    """Trailing stop: long = lowest low of the last N candles, short = highest high. Only moves in the
    trade's favour (long: up, short: down); never looser than the current stop. Returns the new stop price,
    or None if it cannot be computed. A None current_stop (max loss switched off) starts from the extreme."""
    if direction == "LONG":
        ext = snap.trail_low
        if not math.isfinite(ext):
            return current_stop
        return ext if current_stop is None else max(current_stop, ext)
    ext = snap.trail_high
    if not math.isfinite(ext):
        return current_stop
    return ext if current_stop is None else min(current_stop, ext)


# --------------------------------------------------------------------------
# SHADOW: the paper copy of the plan now lives in app/shadow_book.py (2026-10-24 - it shadows the real trade all
# the way and keeps running while real trading is paused). The older candle-only paper engine was removed.
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# ENTRY WAIT MACHINE (pure, one step per closed candle)
# --------------------------------------------------------------------------
@dataclass
class WaitState:
    pend_dir: int = 0          # +1 / -1 / 0
    pend_bars: int = 0
    pend_from_flip: bool = False


def wait_step(ws: WaitState, *, flipped_to: int, reverse: bool, flat: bool, dir_e: int, dir_f: int,
              first_entry_ok: bool, entry_wait: int, flip_wait: int | None = None) -> int:
    """One closed candle of the entry-wait machine (same as the Pine ENTRY WAIT MACHINE).
      flipped_to: direction of the new side if a position was just closed by a flip this candle (else 0)
      reverse   : plan.flip_action is "Reverse"
      flat      : the position is flat (after any flip close this candle)
    Returns the direction to open NOW (+1 / -1) or 0."""
    close_dir = flipped_to if (flipped_to != 0 and reverse) else 0
    if close_dir != 0:
        ws.pend_dir, ws.pend_bars, ws.pend_from_flip = close_dir, 0, True
    else:
        if ws.pend_dir != 0:
            sig = dir_f if ws.pend_from_flip else dir_e
            if flat and sig == ws.pend_dir:
                ws.pend_bars += 1
            else:
                ws.pend_dir = 0
        if ws.pend_dir == 0 and flat and dir_e != 0 and first_entry_ok:
            ws.pend_dir, ws.pend_bars, ws.pend_from_flip = dir_e, 0, False
    need = (entry_wait if flip_wait is None else flip_wait) if ws.pend_from_flip else entry_wait
    if ws.pend_dir != 0 and ws.pend_bars >= need:
        d = ws.pend_dir
        ws.pend_dir = 0
        return d
    return 0


# ==========================================================================
# CHANGE AN OPEN TRADE (owner 2026-11-05)
# ==========================================================================
# The settings that may be changed while a trade is open (they are about HOW the open trade is protected / closed).
# Everything else (entry signals + indicators, flip rules, Indicator Combination, entry size, leverage, chart type,
# timeframe, ...) is part of how the trade was OPENED: it waits for the next trade.
AMEND_PATHS = (
    ("trail",), ("sl",), ("be",), ("tp",), ("exit_when",), ("hold", "adx_level"),
    ("sizing", "stop_on"), ("sizing", "stop_basis"), ("sizing", "stop_pct"), ("sizing", "stop_usd"),
)
AMEND_GROUPS = (       # (group name shown to the owner, the paths in it)
    ("Trailing Stop", (("trail",),)),
    ("Stop Loss", (("sl",),)),
    ("Break-Even", (("be",),)),
    ("Take profit", (("tp",),)),
    ("Exit when", (("exit_when",),)),
    ("Hold rule ADX level", (("hold", "adx_level"),)),
    ("Max Loss per Trade", (("sizing", "stop_on"), ("sizing", "stop_basis"),
                            ("sizing", "stop_pct"), ("sizing", "stop_usd"))),
)
AMEND_NOW, AMEND_NEXT = "now", "next"
AMEND_TIGHTER, AMEND_LOOSER = "tighter", "looser"


def _amend_get(plan: dict, path: tuple):
    cur = plan
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def amend_diff(old: dict, new: dict) -> dict:
    """What differs between the plan the open trade runs on (old) and the newly saved plan (new), both complete.
    {"now": [group names that changed and may be applied to the open trade],
     "waits": True if something else (not allowed on an open trade) also changed, so it waits for the next trade}.
    The strategy name is a label only."""
    old_c, new_c = complete_plan(old), complete_plan(new)
    now = [name for name, paths in AMEND_GROUPS
           if any(_amend_get(old_c, p) != _amend_get(new_c, p) for p in paths)]
    merged = amend_merge(old_c, new_c)
    a = {k: v for k, v in merged.items() if k != "name"}
    b = {k: v for k, v in new_c.items() if k != "name"}
    return {"now": now, "waits": a != b}


def amend_merge(old: dict, new: dict) -> dict:
    """The plan the open trade runs on after the change: a copy of OLD with only the allowed settings taken from NEW."""
    import copy as _copy
    out = _copy.deepcopy(complete_plan(old))
    new_c = complete_plan(new)
    for path in AMEND_PATHS:
        val = _amend_get(new_c, path)
        if val is None and path[-1] not in ("stop_on",):
            continue
        cur = out
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = _copy.deepcopy(val)
    return out
