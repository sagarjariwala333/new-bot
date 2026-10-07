"""
strategy.py
===========

Strategy calculations (live-bot translation).

Everything in this file is PURE CALCULATION (no exchange calls, no I/O), so
it is exchange-neutral: Binance and OKX both call exactly this code. Order
placement lives in app/instance.py and the exchange adapters.

Every numeric setting used here (indicator lengths, levels, allocations,
leverage, stop and tracker multiples, shadow settings) is supplied by the
owner through the dashboard (see app/default_settings.py). Nothing in this
file supplies a default value for any of them.

Data
  * Smoothed candles (derived from the normal candles) drive: candle
    direction, HMA, the filter EMA, the sizing EMA and ATR.
  * NORMAL candles drive: ADX/DMI, execution prices, sizing price, stop
    price and the TP touch checks.

Signals
  LONG  = candle direction up   AND HMA rising  AND close above the filter EMA
  SHORT = candle direction down AND HMA falling AND close below the filter EMA
  No crossover requirement. No ADX filter on entries.

Sizing
  The sizing EMA only picks the allocation / leverage (trend-aligned vs
  counter-trend); it never blocks an entry.
  notional = balance * allocation% * leverage ;  qty = notional / normal close

Stop
  stop_dollars = balance_at_entry * stop%
  LONG : stop = entry - stop_dollars / qty
  SHORT: stop = entry + stop_dollars / qty
  Fixed for the life of the trade. Never trailed, widened or tightened.
  (Live bot: entry = real fill price, qty = real filled quantity.)

TP1 / TP2
  TRACKING ONLY - never an order, never a partial close.

Hold Rule
  In a position and the opposite signal appears: HOLD (ignore it) if the
  Hold Rule is ON, open profit > 0 and ADX is below the configured level.
  Otherwise CLOSE the position at this candle's close.

Flips
  The opposite position is NOT opened on the same candle. On the next
  closed candle the bot is flat and uses the normal entry rules.

Shadow mode
  After a REAL winning trade closes and sets a new closed-equity high, the
  next N trades are paper-only with the same rules (see shadow_step()).
  The closed-equity high is tracked by app/base_v3_tracker.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

STRATEGY_NAME = "BASE_V3"

# How many CLOSED candles the live bot loads for indicator calculation
# (long enough for every indicator to be fully warmed up).
HISTORY_CANDLES = 1000


# ==========================================================================
# PARAMETERS
# ==========================================================================
@dataclass
class StrategyParams:
    """Every field is REQUIRED - there are deliberately no default values.
    Values come from the owner's confirmed dashboard settings (see
    app/default_settings.py)."""
    # Strategy
    hma_length: int
    ema_filter_length: int
    ema_sizing_length: int
    atr_length: int

    # Hold Rule
    use_hold_rule: bool
    adx_di_length: int
    adx_smoothing: int
    hold_adx_level: float

    # Sizing
    trend_alloc_pct: float
    counter_alloc_pct: float
    leverage: float             # leverage for TREND-ALIGNED trades
    counter_leverage: float     # leverage for COUNTER-TREND trades

    # Risk - stop is always on in the live bot
    stop_loss_pct_equity: float

    # TP tracker - tracking only
    tp1_atr_mult: float
    tp2_atr_mult: float

    # Shadow mode
    use_shadow: bool
    shadow_trades: int
    # Applied to the PAPER entry price only (used by the paper Hold Rule).
    shadow_slippage_ticks: int

    @property
    def min_bars_required(self) -> int:
        """Minimum closed candles before the bot will act. Set to the full
        HISTORY_CANDLES so every indicator is fully warmed up. A symbol with
        less history simply waits."""
        return HISTORY_CANDLES


# ==========================================================================
# PINE-EXACT SERIES HELPERS (numpy, NaN = Pine `na`)
# ==========================================================================
def _to_np(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def pine_sma(src, length: int) -> np.ndarray:
    """ta.sma: na unless the last `length` values are all non-na."""
    s = _to_np(src)
    n = len(s)
    out = np.full(n, np.nan)
    if length <= 0:
        return out
    for i in range(length - 1, n):
        w = s[i - length + 1:i + 1]
        if not np.isnan(w).any():
            out[i] = w.mean()
    return out


def pine_rma(src, length: int) -> np.ndarray:
    """ta.rma exactly as documented by TradingView:
        alpha = 1/length
        sum := na(sum[1]) ? ta.sma(src, length) : alpha*src + (1-alpha)*nz(sum[1])
    """
    return _recursive_ma(src, length, 1.0 / length)


def pine_ema(src, length: int) -> np.ndarray:
    """ta.ema - alpha = 2/(length+1), seeded with the SMA of the first
    `length` values (TradingView's built-in behaviour: na for the first
    length-1 bars). With HISTORY_CANDLES=1000 the seed choice has no
    practical effect (see module docstring)."""
    return _recursive_ma(src, length, 2.0 / (length + 1))


def _recursive_ma(src, length: int, alpha: float) -> np.ndarray:
    s = _to_np(src)
    n = len(s)
    out = np.full(n, np.nan)
    sma = pine_sma(s, length)
    prev = np.nan
    for i in range(n):
        if np.isnan(prev):
            val = sma[i]
        else:
            val = alpha * s[i] + (1.0 - alpha) * prev   # na if s[i] is na (Pine)
        out[i] = val
        prev = val
    return out


def pine_wma(src, length: int) -> np.ndarray:
    """ta.wma: weights length..1, newest bar heaviest; na if any value in
    the window is na."""
    s = _to_np(src)
    n = len(s)
    out = np.full(n, np.nan)
    if length <= 0:
        return out
    weights = np.arange(1, length + 1, dtype=float)   # oldest=1 ... newest=length
    norm = weights.sum()
    for i in range(length - 1, n):
        w = s[i - length + 1:i + 1]
        if not np.isnan(w).any():
            out[i] = float(np.dot(w, weights) / norm)
    return out


def pine_hma(src, length: int) -> np.ndarray:
    """Exactly the Pine script's manual HMA (math.floor, not round)."""
    half = int(math.floor(length / 2))
    root = int(math.floor(math.sqrt(length)))
    raw = 2.0 * pine_wma(src, half) - pine_wma(src, length)
    return pine_wma(raw, root)


def _shift(a: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if k < len(a):
        out[k:] = a[:-k] if k > 0 else a
    return out


def _nanmax3(a, b, c) -> np.ndarray:
    """Pine math.max(): na if ANY argument is na."""
    stacked = np.vstack([a, b, c])
    out = np.max(stacked, axis=0)
    out[np.isnan(stacked).any(axis=0)] = np.nan
    return out


def _fixnan(a: np.ndarray) -> np.ndarray:
    """Pine fixnan(): replace na with the last non-na value."""
    out = a.copy()
    last = np.nan
    for i in range(len(out)):
        if np.isnan(out[i]):
            out[i] = last
        else:
            last = out[i]
    return out


# ==========================================================================
# SMOOTHED CANDLES (derived from the normal candles)
# ==========================================================================
def smoothed_candles(df: pd.DataFrame) -> pd.DataFrame:
    """sc_close = (o+h+l+c)/4
       sc_open  = first bar: (o+c)/2 ; after: (sc_open[1] + sc_close[1]) / 2
       sc_high  = max(h, sc_open, sc_close)
       sc_low   = min(l, sc_open, sc_close)"""
    o = _to_np(df["open"])
    h = _to_np(df["high"])
    l = _to_np(df["low"])
    c = _to_np(df["close"])
    n = len(df)
    sc_close = (o + h + l + c) / 4.0
    sc_open = np.empty(n)
    for i in range(n):
        sc_open[i] = (o[i] + c[i]) / 2.0 if i == 0 else (sc_open[i - 1] + sc_close[i - 1]) / 2.0
    sc_high = np.maximum.reduce([h, sc_open, sc_close])
    sc_low = np.minimum.reduce([l, sc_open, sc_close])
    return pd.DataFrame({"sc_open": sc_open, "sc_high": sc_high,
                         "sc_low": sc_low, "sc_close": sc_close}, index=df.index)


# ==========================================================================
# ATR FROM THE SMOOTHED CANDLES
# ==========================================================================
def atr_from_ha(ha: pd.DataFrame, length: int) -> np.ndarray:
    hh = _to_np(ha["sc_high"])
    hl = _to_np(ha["sc_low"])
    hc_prev = _shift(_to_np(ha["sc_close"]))
    tr = _nanmax3(hh - hl, np.abs(hh - hc_prev), np.abs(hl - hc_prev))
    return pine_rma(tr, length)


# ==========================================================================
# DMI / ADX FROM NORMAL CANDLES (Pine ta.dmi(diLength, adxSmoothing))
# ==========================================================================
def pine_dmi(df: pd.DataFrame, di_length: int, adx_smoothing: int):
    """TradingView ta.dmi source, translated line by line:
        up = ta.change(high) ; down = -ta.change(low)
        plusDM  = na(up)   ? na : (up > down and up > 0 ? up : 0)
        minusDM = na(down) ? na : (down > up and down > 0 ? down : 0)
        truerange = ta.rma(ta.tr, len)
        plus  = fixnan(100 * ta.rma(plusDM, len) / truerange)
        minus = fixnan(100 * ta.rma(minusDM, len) / truerange)
        sum = plus + minus
        adx = 100 * ta.rma(math.abs(plus - minus) / (sum == 0 ? 1 : sum), adxLen)
    ta.tr is na on the very first bar (no previous close)."""
    h = _to_np(df["high"])
    l = _to_np(df["low"])
    c = _to_np(df["close"])
    up = h - _shift(h)
    down = -(l - _shift(l))
    plus_dm = np.where(np.isnan(up), np.nan, np.where((up > down) & (up > 0), up, 0.0))
    minus_dm = np.where(np.isnan(down), np.nan, np.where((down > up) & (down > 0), down, 0.0))
    c_prev = _shift(c)
    tr = _nanmax3(h - l, np.abs(h - c_prev), np.abs(l - c_prev))
    truerange = pine_rma(tr, di_length)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus = _fixnan(100.0 * pine_rma(plus_dm, di_length) / truerange)
        minus = _fixnan(100.0 * pine_rma(minus_dm, di_length) / truerange)
        s = plus + minus
        denom = np.where(s == 0, 1.0, s)
        adx = 100.0 * pine_rma(np.abs(plus - minus) / denom, adx_smoothing)
    return plus, minus, adx


# ==========================================================================
# INDICATORS + SIGNALS
# ==========================================================================
def compute_indicators(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """df: NORMAL closed candles, oldest -> newest, columns open_time/open/
    high/low/close. Returns a copy with every Base V3 column added."""
    out = df.copy().reset_index(drop=True)
    ha = smoothed_candles(out)
    for col in ha.columns:
        out[col] = ha[col].values

    sc_close = out["sc_close"].values
    out["sc_up"] = out["sc_close"] > out["sc_open"]
    out["sc_down"] = out["sc_close"] < out["sc_open"]

    hma = pine_hma(sc_close, p.hma_length)
    hma_prev = _shift(hma)
    out["hma"] = hma
    out["hma_prev"] = hma_prev
    # NaN comparisons are False, same as Pine's na comparisons in a condition
    out["hma_up"] = hma > hma_prev
    out["hma_down"] = hma < hma_prev

    out["ema_filter"] = pine_ema(sc_close, p.ema_filter_length)
    out["ema_sizing"] = pine_ema(sc_close, p.ema_sizing_length)
    out["atr_sc"] = atr_from_ha(out, p.atr_length)

    di_plus, di_minus, adx = pine_dmi(out, p.adx_di_length, p.adx_smoothing)
    out["di_plus"] = di_plus
    out["di_minus"] = di_minus
    out["adx"] = adx

    out["long_signal"] = out["sc_up"] & out["hma_up"] & (out["sc_close"] > out["ema_filter"])
    out["short_signal"] = out["sc_down"] & out["hma_down"] & (out["sc_close"] < out["ema_filter"])

    long_trend = out["sc_close"] > out["ema_sizing"]
    short_trend = out["sc_close"] < out["ema_sizing"]
    out["long_alloc_pct"] = np.where(long_trend, p.trend_alloc_pct, p.counter_alloc_pct)
    out["short_alloc_pct"] = np.where(short_trend, p.trend_alloc_pct, p.counter_alloc_pct)
    return out


@dataclass
class IndicatorSnapshot:
    """Everything the bot needs from ONE closed candle (the latest)."""
    open_time: int
    # normal candle
    open: float
    high: float
    low: float
    close: float
    # smoothed candles
    sc_open: float
    sc_high: float
    sc_low: float
    sc_close: float
    sc_up: bool
    sc_down: bool
    # indicators
    hma: float
    hma_prev: float
    hma_up: bool
    hma_down: bool
    ema_filter: float
    ema_sizing: float
    atr_sc: float
    di_plus: float
    di_minus: float
    adx: float
    # decisions
    long_signal: bool
    short_signal: bool
    long_alloc_pct: float
    short_alloc_pct: float

    def to_display_dict(self) -> dict:
        """For the dashboard runtime panel."""
        signal = "LONG" if self.long_signal else ("SHORT" if self.short_signal else "NONE")
        hma_dir = "UP" if self.hma_up else ("DOWN" if self.hma_down else "FLAT")
        sc_direction = "UP" if self.sc_up else ("DOWN" if self.sc_down else "FLAT")
        return {
            "candle_open_time": self.open_time,
            "close": self.close, "high": self.high, "low": self.low,
            "sc_open": self.sc_open, "sc_close": self.sc_close, "sc_direction": sc_direction,
            "hma": self.hma, "hma_direction": hma_dir,
            "ema_filter": self.ema_filter, "ema_filter_side": "ABOVE" if self.sc_close > self.ema_filter else "BELOW",
            "ema_sizing": self.ema_sizing, "ema_sizing_side": "ABOVE" if self.sc_close > self.ema_sizing else "BELOW",
            "atr_sc": self.atr_sc, "adx": self.adx, "di_plus": self.di_plus, "di_minus": self.di_minus,
            "signal": signal,
            "long_alloc_pct": self.long_alloc_pct, "short_alloc_pct": self.short_alloc_pct,
        }


def _f(x) -> float:
    return float(x) if x is not None else float("nan")


def snapshot_at(df_ind: pd.DataFrame, i: int) -> IndicatorSnapshot:
    row = df_ind.iloc[i]
    return IndicatorSnapshot(
        open_time=int(row["open_time"]),
        open=_f(row["open"]), high=_f(row["high"]), low=_f(row["low"]), close=_f(row["close"]),
        sc_open=_f(row["sc_open"]), sc_high=_f(row["sc_high"]), sc_low=_f(row["sc_low"]),
        sc_close=_f(row["sc_close"]),
        sc_up=bool(row["sc_up"]), sc_down=bool(row["sc_down"]),
        hma=_f(row["hma"]), hma_prev=_f(row["hma_prev"]),
        hma_up=bool(row["hma_up"]), hma_down=bool(row["hma_down"]),
        ema_filter=_f(row["ema_filter"]), ema_sizing=_f(row["ema_sizing"]), atr_sc=_f(row["atr_sc"]),
        di_plus=_f(row["di_plus"]), di_minus=_f(row["di_minus"]), adx=_f(row["adx"]),
        long_signal=bool(row["long_signal"]), short_signal=bool(row["short_signal"]),
        long_alloc_pct=_f(row["long_alloc_pct"]), short_alloc_pct=_f(row["short_alloc_pct"]),
    )


def last_snapshot(df_ind: pd.DataFrame) -> IndicatorSnapshot:
    return snapshot_at(df_ind, len(df_ind) - 1)


# ==========================================================================
# DECISIONS
# ==========================================================================
def entry_direction(snap: IndicatorSnapshot) -> str | None:
    """Pine: `if longSignal ... else if shortSignal` (long checked first;
    both can never be true together since up/down are exclusive)."""
    if snap.long_signal:
        return "LONG"
    if snap.short_signal:
        return "SHORT"
    return None


def allocation_pct(direction: str, snap: IndicatorSnapshot) -> float:
    if direction == "LONG":
        return snap.long_alloc_pct
    if direction == "SHORT":
        return snap.short_alloc_pct
    raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")


def trade_leverage(direction: str, snap: IndicatorSnapshot, p: StrategyParams) -> float:
    """Leverage for THIS trade. LONG is trend-aligned when sc_close > sizing EMA,
    SHORT when sc_close < sizing EMA; trend-aligned trades use `leverage`,
    counter-trend trades use `counterLeverage`. Same test (and same NaN behaviour:
    a NaN sizing EMA is never 'trend-aligned') as the allocation column in
    compute_indicators, so allocation and leverage always agree on trade type."""
    if direction == "LONG":
        trend_aligned = snap.sc_close > snap.ema_sizing
    elif direction == "SHORT":
        trend_aligned = snap.sc_close < snap.ema_sizing
    else:
        raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")
    return p.leverage if trend_aligned else p.counter_leverage


# ==========================================================================
# FIRST-REVERSAL RULE
# ==========================================================================
# A brand-new pair must not take its FIRST trade mid-trend. Signals are state based (they are
# true on many consecutive candles), so "a signal is on" says nothing about how old the trend is.
# The first real entry therefore waits for a fresh REVERSAL: a candle whose signal direction is the
# OPPOSITE of the most recent signal before it (candles with no signal in between do not matter:
# LONG, none, SHORT is a reversal; LONG, none, LONG is not).
def signal_direction_at(ind, i: int) -> str | None:
    """LONG / SHORT / None for candle i of an indicator frame (needs long_signal / short_signal)."""
    if i < 0 or i >= len(ind):
        return None
    if bool(ind["long_signal"].iloc[i]):
        return "LONG"
    if bool(ind["short_signal"].iloc[i]):
        return "SHORT"
    return None


def last_signal_direction(ind, upto: int) -> str | None:
    """Direction of the most recent signal at or before index `upto` (None if there never was one)."""
    for i in range(min(upto, len(ind) - 1), -1, -1):
        d = signal_direction_at(ind, i)
        if d is not None:
            return d
    return None


def is_fresh_reversal(ind) -> bool:
    """True when the LATEST closed candle carries a signal whose direction differs from the most
    recent signal before it (or is the very first signal in the history)."""
    i = len(ind) - 1
    cur = signal_direction_at(ind, i)
    if cur is None:
        return False
    prev = last_signal_direction(ind, i - 1)
    return prev is None or prev != cur


def position_notional(balance: float, alloc_pct: float, leverage: float) -> float:
    """Pine: notional = strategy.equity * (allocPct/100) * leverage."""
    return balance * (alloc_pct / 100.0) * leverage


def position_qty(balance: float, alloc_pct: float, leverage: float, price: float) -> float:
    """Pine: qty = notional / normalClose (0 if price <= 0)."""
    if price <= 0:
        return 0.0
    return position_notional(balance, alloc_pct, leverage) / price


def stop_loss_dollars(balance_at_entry: float, p: StrategyParams) -> float:
    """Pine: slLossLimit = entryEquity * (stopLossPctEq / 100)."""
    return balance_at_entry * (p.stop_loss_pct_equity / 100.0)


def stop_price(direction: str, entry_price: float, balance_at_entry: float, qty: float,
               p: StrategyParams) -> float:
    """Pine: stop = entryPrice -/+ slLossLimit / qty. Live bot passes the REAL
    fill price and the REAL filled quantity."""
    if qty <= 0:
        raise ValueError("qty must be > 0 to compute the Base V3 stop")
    dist = stop_loss_dollars(balance_at_entry, p) / abs(qty)
    if direction == "LONG":
        return entry_price - dist
    if direction == "SHORT":
        return entry_price + dist
    raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")


def tp_levels(direction: str, entry_price: float, entry_atr: float, p: StrategyParams) -> tuple[float, float]:
    """Pine: tp1Price/tp2Price. Tracking only - never an order."""
    if direction == "LONG":
        return entry_price + p.tp1_atr_mult * entry_atr, entry_price + p.tp2_atr_mult * entry_atr
    if direction == "SHORT":
        return entry_price - p.tp1_atr_mult * entry_atr, entry_price - p.tp2_atr_mult * entry_atr
    raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")


def update_tp_touched(direction: str, high: float, low: float, tp1: float | None, tp2: float | None,
                      tp1_touched: bool, tp2_touched: bool) -> tuple[bool, bool, bool, bool]:
    """Pine: `if not tp1Touched and normalHigh >= tp1Price` (long) /
    `normalLow <= tp1Price` (short). Returns (tp1_touched, tp2_touched,
    tp1_newly, tp2_newly). Never closes anything."""
    new1 = new2 = False
    if tp1 is not None and not tp1_touched:
        if (direction == "LONG" and high >= tp1) or (direction == "SHORT" and low <= tp1):
            tp1_touched, new1 = True, True
    if tp2 is not None and not tp2_touched:
        if (direction == "LONG" and high >= tp2) or (direction == "SHORT" and low <= tp2):
            tp2_touched, new2 = True, True
    return tp1_touched, tp2_touched, new1, new2


def open_profit_sign(direction: str, entry_price: float, close: float) -> float:
    """Open profit at the candle close, per unit (the sign is what the Hold
    Rule uses - Pine: strategy.openprofit > 0)."""
    return (close - entry_price) if direction == "LONG" else (entry_price - close)


def hold_rule_holds(direction: str, snap: IndicatorSnapshot, entry_price: float, p: StrategyParams) -> bool:
    """Pine: holdLong  = useHoldRule and shortSignal and openprofit > 0 and adx < level
             holdShort = useHoldRule and longSignal  and openprofit > 0 and adx < level"""
    opposite = snap.short_signal if direction == "LONG" else snap.long_signal
    if not (p.use_hold_rule and opposite):
        return False
    if not (open_profit_sign(direction, entry_price, snap.close) > 0):
        return False
    return bool(snap.adx < p.hold_adx_level)   # NaN ADX -> False (not held), same as Pine


def in_position_action(direction: str, snap: IndicatorSnapshot, entry_price: float,
                       p: StrategyParams) -> str:
    """Returns "CLOSE" (opposite signal, not held), "HOLD" (opposite signal
    held by the Hold Rule) or "STAY" (no opposite signal)."""
    opposite = snap.short_signal if direction == "LONG" else snap.long_signal
    if not opposite:
        return "STAY"
    if hold_rule_holds(direction, snap, entry_price, p):
        return "HOLD"
    return "CLOSE"


# ==========================================================================
# SHADOW MODE (Pine: "SHADOW MODE AFTER A NEW EQUITY HIGH")
# ==========================================================================
@dataclass
class ShadowTradeRecord:
    direction: str
    entry_time: int
    entry_price: float          # paper entry incl. the configured slippage
    signal_close: float         # the normal close the paper trade was opened at
    stop_price: float
    exit_time: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None   # "STOP" | "FLIP"
    result_pct: float | None = None  # % of balance, for the tracker tab only


@dataclass
class ShadowState:
    active: bool = False
    count: int = 0                     # paper trades CLOSED in the current period
    pos: int = 0                       # 0 flat, 1 long, -1 short
    entry: float | None = None         # paper entry price (with slippage)
    signal_close: float | None = None
    stop: float | None = None
    alloc_pct: float | None = None
    leverage: float | None = None      # leverage the open paper trade was sized/stopped with
    entry_time: int | None = None      # candle open_time of the paper entry (Pine shEntryBar)
    resume_block_time: int | None = None   # Pine resumeBlockBar
    periods: int = 0
    trades_total: int = 0
    last_end_reason: str | None = None
    current: dict | None = None        # ShadowTradeRecord as dict while a paper trade is open
    history: list = field(default_factory=list)


def shadow_activate(state: ShadowState) -> None:
    """Pine: shadowActive := true ; shCount := 0 ; shPos := 0 ; shadowPeriods += 1"""
    state.active = True
    state.count = 0
    state.pos = 0
    state.entry = None
    state.signal_close = None
    state.stop = None
    state.alloc_pct = None
    state.leverage = None
    state.entry_time = None
    state.current = None
    state.periods += 1
    state.last_end_reason = None


def _paper_result_pct(direction: int, signal_close: float, exit_price: float, alloc_pct: float,
                      leverage: float) -> float:
    """% of balance a paper trade made/lost (display only, never used by
    any decision): price move % x allocation x leverage."""
    move = (exit_price - signal_close) / signal_close if direction == 1 else (signal_close - exit_price) / signal_close
    return move * (alloc_pct / 100.0) * leverage * 100.0


def _close_paper(state: ShadowState, exit_time: int, exit_price: float, reason: str,
                 p: StrategyParams, max_history: int) -> None:
    rec = dict(state.current or {})
    if rec:
        rec["exit_time"] = exit_time
        rec["exit_price"] = exit_price
        rec["exit_reason"] = reason
        try:
            rec["result_pct"] = _paper_result_pct(state.pos, rec["signal_close"], exit_price,
                                                  state.alloc_pct or 0.0, state.leverage or p.leverage)
        except (TypeError, ZeroDivisionError):
            rec["result_pct"] = None
        state.history.append(rec)
        if len(state.history) > max_history:
            del state.history[: len(state.history) - max_history]
    state.pos = 0
    state.current = None
    state.count += 1
    state.trades_total += 1


def shadow_step(state: ShadowState, snap: IndicatorSnapshot, p: StrategyParams, tick_size: float,
                max_history: int = 500) -> list[str]:
    """One closed candle of the Pine shadow block, in the same order:

      if shadowActive
          1) paper stop hit during this candle (only from the candle AFTER entry)
          if shCount >= shadowTrades -> shadowActive := false
          else
              2) same decisions as the real trades, at candle close:
                 flat  -> enter on longSignal / shortSignal
                 long  -> close on shortSignal unless held (close > entry and ADX < level)
                 short -> close on longSignal  unless held (close < entry and ADX < level)
                 if the Nth paper trade closed by flip -> shadowActive := false and
                 block real entries on this same candle (resumeBlockBar)

    Returns human-readable event strings for logging."""
    events: list[str] = []
    if not state.active:
        return events
    t = snap.open_time
    slip = p.shadow_slippage_ticks * tick_size

    # 1) paper stop (Pine: shPos != 0 and useStopLoss and bar_index > shEntryBar)
    if state.pos != 0 and state.entry_time is not None and t > state.entry_time and state.stop is not None:
        if (state.pos == 1 and snap.low <= state.stop) or (state.pos == -1 and snap.high >= state.stop):
            side = "LONG" if state.pos == 1 else "SHORT"
            _close_paper(state, t, state.stop, "STOP", p, max_history)
            events.append(f"paper {side} stopped at {state.stop} (paper trade {state.count}/{p.shadow_trades})")

    if state.count >= p.shadow_trades:
        state.active = False
        state.last_end_reason = "last paper trade stopped out"
        events.append("shadow mode ended - real trading resumes (same candle, as in Pine after a stop)")
        return events

    # 2) same decisions as the real trades, at candle close
    if state.pos == 0:
        if snap.long_signal:
            state.pos = 1
            state.signal_close = snap.close
            state.entry = snap.close + slip
            state.alloc_pct = snap.long_alloc_pct
            state.leverage = trade_leverage("LONG", snap, p)
            state.stop = snap.close * (1 - (p.stop_loss_pct_equity / 100.0) /
                                       (snap.long_alloc_pct / 100.0 * state.leverage))
            state.entry_time = t
            state.current = asdict(ShadowTradeRecord("LONG", t, state.entry, snap.close, state.stop))
            events.append(f"paper LONG opened @ {snap.close} (stop {state.stop:.8g})")
        elif snap.short_signal:
            state.pos = -1
            state.signal_close = snap.close
            state.entry = snap.close - slip
            state.alloc_pct = snap.short_alloc_pct
            state.leverage = trade_leverage("SHORT", snap, p)
            state.stop = snap.close * (1 + (p.stop_loss_pct_equity / 100.0) /
                                       (snap.short_alloc_pct / 100.0 * state.leverage))
            state.entry_time = t
            state.current = asdict(ShadowTradeRecord("SHORT", t, state.entry, snap.close, state.stop))
            events.append(f"paper SHORT opened @ {snap.close} (stop {state.stop:.8g})")
    elif state.pos == 1:
        held = p.use_hold_rule and snap.short_signal and snap.close > state.entry and snap.adx < p.hold_adx_level
        if snap.short_signal and not held:
            _close_paper(state, t, snap.close, "FLIP", p, max_history)
            events.append(f"paper LONG closed by flip @ {snap.close} (paper trade {state.count}/{p.shadow_trades})")
            if state.count >= p.shadow_trades:
                state.active = False
                state.resume_block_time = t
                state.last_end_reason = "last paper trade closed by flip"
                events.append("shadow mode ended - real trading resumes from the NEXT candle")
    elif state.pos == -1:
        held = p.use_hold_rule and snap.long_signal and snap.close < state.entry and snap.adx < p.hold_adx_level
        if snap.long_signal and not held:
            _close_paper(state, t, snap.close, "FLIP", p, max_history)
            events.append(f"paper SHORT closed by flip @ {snap.close} (paper trade {state.count}/{p.shadow_trades})")
            if state.count >= p.shadow_trades:
                state.active = False
                state.resume_block_time = t
                state.last_end_reason = "last paper trade closed by flip"
                events.append("shadow mode ended - real trading resumes from the NEXT candle")
    return events


def real_entry_allowed(state: ShadowState, candle_open_time: int) -> bool:
    """Pine: realAllowed = not shadowActive and bar_index != resumeBlockBar"""
    return (not state.active) and (state.resume_block_time != candle_open_time)
