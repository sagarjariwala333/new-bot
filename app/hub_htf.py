"""
hub_htf.py - the other-timeframe candles the Exit-when DIVERGENCE oscillators read (owner 2026-11-17: each oscillator on its own
timeframe, open to every candle size, e.g. a 3-day RSI on an 8h chart, or a 1m MACD on a 5m chart for scalping).

Only candles that have CLOSED by the close of the latest closed chart candle are returned, so nothing in the future is ever read.
The plan decides which candle sizes are needed (strategy_hub.div_tfs_needed); with no divergence oscillator on another timeframe
nothing is fetched. Candles are the EXCHANGE's own (Binance / OKX), so a 3D or 1W candle can start on a different day than
TradingView's: results can differ a little from the Pine test.
"""
from __future__ import annotations

import math

import pandas as pd

from app import strategy_hub as hub
from app.candle_cache import cached_klines

COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "qav", "trades", "tbbav", "tbqav", "ignore"]
LIVE_CAP = 1500        # live: one request (the cache keeps it cheap); only the recent candles matter there
ANALYSIS_CAP = 5000    # Analysis tab: up to this many candles per timeframe (same cap as the chart candles there)
WARM = 300             # extra candles of that size, so the oscillators are warmed up when the chart history starts


def _frame(raw) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=COLS)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = df["open_time"].astype("int64")
    return df


def chart_ms_of(closed: pd.DataFrame) -> int:
    t = closed["open_time"].to_numpy(dtype="int64")
    if len(t) < 2:
        return 0
    return int(pd.Series(t[1:] - t[:-1]).median())


async def fetch_htf(client, symbol: str, plan: dict, closed: pd.DataFrame, live: bool = True) -> dict:
    """{candle size: DataFrame of CLOSED candles of that size} for every other timeframe the plan's divergence oscillators read."""
    tfs = hub.div_tfs_needed(plan)
    if not tfs or closed is None or not len(closed):
        return {}
    cms = chart_ms_of(closed)
    last_close = int(closed["open_time"].iloc[-1]) + cms          # the close of the latest closed chart candle
    first_open = int(closed["open_time"].iloc[0])
    out = {}
    for tf in tfs:
        tf_ms = hub.DIV_TF_MS[tf]
        need = int(math.ceil(max(0, last_close - first_open) / tf_ms)) + WARM
        limit = min(LIVE_CAP if live else ANALYSIS_CAP, max(need, 200))
        raw = await (cached_klines(client, symbol, tf, limit) if live else client.get_klines(symbol, tf, limit=limit))
        df = _frame(raw)
        out[tf] = df[hub.div_tf_close(tf, df["open_time"].to_numpy()) <= last_close].reset_index(drop=True)
    return out
