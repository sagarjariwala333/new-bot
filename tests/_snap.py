"""
_snap.py
========

Test helper: builds an IndicatorSnapshot for the execution-layer tests.
Many infrastructure tests only need "a snapshot that says LONG/SHORT at price X".
The helper accepts short aliases the infrastructure tests use:
  long_condition  -> long_signal
  short_condition -> short_signal
  atr             -> atr_sc
Values are arbitrary test data (see _test_values.py).
"""

from app import strategy as strat
from tests import _test_values as tv


def make_snap(close=100.0, high=None, low=None, open_=None, long_condition=None, short_condition=None,
              long_signal=False, short_signal=False, atr=None, atr_sc=2.0, adx=30.0,
              di_plus=30.0, di_minus=10.0, open_time=1,
              long_alloc_pct=None, short_alloc_pct=None,
              ema_filter=None, ema_sizing=None) -> strat.IndicatorSnapshot:
    if long_condition is not None:
        long_signal = long_condition
    if short_condition is not None:
        short_signal = short_condition
    if atr is not None:
        atr_sc = atr
    if long_alloc_pct is None:
        long_alloc_pct = tv.STRATEGY_VALUES["trend_alloc_pct"]
    if short_alloc_pct is None:
        short_alloc_pct = tv.STRATEGY_VALUES["trend_alloc_pct"]
    high = close + 0.5 if high is None else high
    low = close - 0.5 if low is None else low
    open_ = close if open_ is None else open_
    return strat.IndicatorSnapshot(
        open_time=open_time, open=open_, high=high, low=low, close=close,
        sc_open=close - (1 if long_signal else -1), sc_high=high, sc_low=low, sc_close=close,
        sc_up=bool(long_signal), sc_down=bool(short_signal),
        hma=close, hma_prev=close - (1 if long_signal else (-1 if short_signal else 0)),
        hma_up=bool(long_signal), hma_down=bool(short_signal),
        ema_filter=(close - 1 if long_signal else close + 1) if ema_filter is None else ema_filter,
        ema_sizing=(close - 1) if ema_sizing is None else ema_sizing,
        atr_sc=atr_sc, di_plus=di_plus, di_minus=di_minus, adx=adx,
        long_signal=bool(long_signal), short_signal=bool(short_signal),
        long_alloc_pct=long_alloc_pct, short_alloc_pct=short_alloc_pct,
    )
