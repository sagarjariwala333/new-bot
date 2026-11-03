"""
hub_backtest.py
===============

ANALYSIS TAB for the STRATEGY HUB (owner 2026-10-24: "When I start the Hub the analysis WILL track all the strategy
created in the hub not following V3.. it will be different strategy for different pairs").

Replays a pair's saved Hub plan over PAST candles with the same engine as the shadow (app/shadow_book.py), so the
rules are the bot's own: entry (Main + confluence), FLIP, entry wait, Allow Mid-Trend Re-entry, first trade on a
fresh reversal, hold rule, Stop Loss (every Measured on choice, Time stop), Break-Even, Max loss, every Trailing Stop type (incl. "Begins at a price move"), partial
take profit, entry size (allocation % or the fixed base amount with its running total and lock-out %).

Inside a candle the live price is not known from history. The path used is TradingView's: an up candle (close >=
open) goes open -> low -> high -> close, a down candle open -> high -> low -> close. A stop the OPEN already went
through fills at the open (a gap), otherwise a stop or a target fills at its own level; a flip closes at the close.

Costs: the owner's commission % and slippage % are taken off every fill (the entry and each exit part), on the
traded size (allocation x leverage).

Shadow mode (trades off): when the plan has Shadow mode picked, a real winning trade that makes a new high starts
"trades off": the next N trades are not counted (paper only), exactly like the bot. Without Shadow mode every
trade counts.

Offline only: no orders, no live state, nothing here feeds a live decision. Pure logic, no I/O.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app import shadow_book as B
from app import strategy_hub as hub

WARMUP = 200          # same as the live bot: indicators need this many closed candles first


class BacktestError(ValueError):
    pass


def _path(o: float, h: float, l: float, c: float) -> list[float]:
    """TradingView's price path inside one candle (the close is handled by the candle close itself)."""
    return [o, l, h] if c >= o else [o, h, l]


def _gap_open(book: dict, price: float, t_ms: int) -> list[dict]:
    """The candle OPENS beyond the nearest stop: the stop fills at the open (not at its level)."""
    if book["pos"] == 0:
        return []
    d = "LONG" if book["pos"] == 1 else "SHORT"
    name, level = hub.nearest_level(d, book["levels"])
    if name is None:
        return []
    beyond = price < level if d == "LONG" else price > level
    if not beyond:
        return []
    return [B._close_all(book, price, hub.LADDER_NAMES[name], t_ms)]


def run(df: pd.DataFrame, plan: dict, initial_equity: float, commission_pct: float, slippage_pct: float,
        tick_size: float = 0.0) -> dict:
    """Replays the plan over df (closed candles, oldest first, columns open_time/open/high/low/close/volume).
    Returns {"trades": [...], "tested_from": index, "candles": n}. Every trade has its net % result on the
    equity at its entry (costs taken off), and counted = False when it fell in a trades-off period."""
    if initial_equity is None or not (initial_equity > 0):
        raise BacktestError("starting equity must be above 0")
    try:
        plan = hub.complete_plan(plan)
    except hub.PlanError as e:
        raise BacktestError(f"the strategy plan of this pair is not complete: {e}")
    n = len(df)
    if n < WARMUP + 50:
        raise BacktestError(f"not enough history: need at least {WARMUP + 50} closed candles, have {n}")
    frame = hub.compute_hub(df.reset_index(drop=True), plan)
    cost_rate = (float(commission_pct) + float(slippage_pct)) / 100.0
    book = B.new_book(1.0)
    plan_paper = dict(plan, shadow=dict(plan["shadow"], slippage_ticks=0))   # slippage comes from the owner's %
    shadow_on = bool(plan["shadow"]["on"])
    off_n = int(plan["shadow"].get("trades") or 0)
    lock = plan["sizing"].get("lock_pct")

    equity = float(initial_equity)
    peak = equity
    last_total = None            # fixed base amount: the running total of the last counted trade
    off_left = 0
    entry_equity = None          # real equity when the open paper trade was entered
    entry_counted = True
    out: list[dict] = []

    def settle(rec: dict, t_ms: int):
        nonlocal equity, peak, last_total, off_left
        legs = rec.get("legs") or []
        notional = (rec.get("alloc_pct") or 0.0) / 100.0 * (rec.get("leverage") or 0.0)
        portions = sum(l["portion"] for l in legs)
        cost_pct = cost_rate * notional * (1.0 + portions) * 100.0
        gross_pct = float(rec.get("result_pct") or 0.0)
        net_pct = gross_pct - cost_pct
        base = entry_equity if entry_equity else equity
        counted = entry_counted
        row = {"direction": rec.get("direction"), "entry_time": rec.get("entry_time"),
               "exit_time": rec.get("exit_time"), "entry_price": rec.get("signal_close"),
               "exit_price": rec.get("exit_price"), "exit_reason": rec.get("exit_reason"),
               "gross_pct": gross_pct, "cost_pct": cost_pct, "net_pct": net_pct, "counted": counted,
               "equity_before": base}
        if counted:
            pnl = base * net_pct / 100.0
            margin = base * (rec.get("alloc_pct") or 0.0) / 100.0
            equity = equity + pnl
            row.update(pnl=pnl, costs=base * cost_pct / 100.0, equity_after=equity)
            if hub.uses_fixed_base(plan):
                last_total = hub.carry_total(margin, pnl, lock)
            if shadow_on and off_n > 0 and pnl > 0 and equity > peak + 1e-12:
                off_left = off_n
            peak = max(peak, equity)
        else:
            row.update(pnl=0.0, costs=0.0, equity_after=equity)
            off_left = max(off_left - 1, 0)
        out.append(row)

    for i in range(WARMUP, n):
        snap = hub.hub_snapshot_at(frame, i)
        t_close = int(snap.open_time) + 1
        closed = []
        if book["pos"] != 0:
            o, h, l = snap.open, snap.high, snap.low
            closed += _gap_open(book, o, t_close)
            for px in _path(o, h, l, snap.close):
                if book["pos"] == 0:
                    break
                closed += B.on_price(book, px, plan_paper, t_close)
        for rec in closed:
            settle(rec, t_close)
        was_open = book["pos"] != 0
        _, closed2 = B.on_candle(book, snap, plan_paper, tick_size=tick_size, last_total=last_total,
                                 balance=equity, t_ms=t_close)
        # on_candle closes first (close price / flip), then may open the next trade on the same close
        opened_now = book["pos"] != 0 and (not was_open or bool(closed2))
        for rec in closed2:
            settle(rec, t_close)
        if opened_now:
            entry_equity = equity
            entry_counted = off_left == 0
    return {"trades": out, "tested_from": WARMUP, "candles": n - WARMUP,
            "first_time": int(df["open_time"].iloc[WARMUP]), "last_time": int(df["open_time"].iloc[-1])}


def _stats(rows: list[dict], start: float) -> dict:
    counted = [r for r in rows if r["counted"]]
    if not counted:
        return {"trades": 0, "skipped": len(rows), "win_rate": None, "total_pnl": 0.0, "total_costs": 0.0,
                "max_drawdown_pct": None}
    eq = start
    curve = [eq]
    pnl_sum = cost_sum = 0.0
    for r in counted:
        p = eq * r["net_pct"] / 100.0
        pnl_sum += p
        cost_sum += eq * r["cost_pct"] / 100.0
        eq += p
        curve.append(eq)
    arr = np.array(curve)
    run_max = np.maximum.accumulate(arr)
    dd = (run_max - arr) / run_max * 100.0
    wins = sum(1 for r in counted if r["net_pct"] > 0)
    return {"trades": len(counted), "skipped": len(rows) - len(counted), "win_rate": wins / len(counted) * 100.0,
            "total_pnl": float(pnl_sum), "total_costs": float(cost_sum), "max_drawdown_pct": float(dd.max())}


def walk_forward(df: pd.DataFrame, plan: dict, n_folds: int, initial_equity: float, commission_pct: float,
                 slippage_pct: float, tick_size: float = 0.0) -> dict:
    """One replay over the whole history (indicators warm, the plan's memory carried on like the bot), then the
    tested candles are cut into n_folds windows one after the other. Each window: its trades (by entry candle),
    win rate, net P&L and max drawdown, each window starting from the starting equity."""
    if n_folds < 2:
        raise BacktestError("at least 2 folds")
    res = run(df, plan, initial_equity, commission_pct, slippage_pct, tick_size)
    times = df["open_time"].astype("int64").to_numpy()[WARMUP:]
    m = len(times)
    size = m // n_folds
    if size < 1:
        raise BacktestError(f"not enough tested candles ({m}) for {n_folds} folds")
    folds = []
    for f in range(n_folds):
        a = f * size
        b = m if f == n_folds - 1 else (f + 1) * size
        t0, t1 = int(times[a]), int(times[b - 1])
        rows = [r for r in res["trades"] if r["entry_time"] is not None and t0 <= r["entry_time"] <= t1]
        folds.append({"fold": f + 1, "candles": b - a, "from_time": t0, "to_time": t1, **_stats(rows, initial_equity)})
    whole = _stats(res["trades"], initial_equity)
    return {"folds": folds, "n_folds": n_folds, "whole": whole, "candles_tested": m,
            "shadow_trades_off": bool(hub.complete_plan(plan)["shadow"]["on"])}


def monte_carlo(df: pd.DataFrame, plan: dict, n_sims: int, initial_equity: float, commission_pct: float,
                slippage_pct: float, seed: int | None = None, tick_size: float = 0.0) -> dict:
    """Reshuffles the counted trades' net % results (with replacement) n_sims times. Results compound on the
    equity, like the bot's sizing."""
    res = run(df, plan, initial_equity, commission_pct, slippage_pct, tick_size)
    rets = np.array([r["net_pct"] / 100.0 for r in res["trades"] if r["counted"]])
    if len(rets) == 0:
        raise BacktestError("no trades in this history - pull more candles or check the strategy")
    rng = np.random.default_rng(seed)
    finals = np.empty(n_sims)
    dds = np.empty(n_sims)
    for s in range(n_sims):
        sample = rng.choice(rets, size=len(rets), replace=True)
        curve = initial_equity * np.cumprod(1.0 + sample)
        curve = np.concatenate([[initial_equity], curve])
        run_max = np.maximum.accumulate(curve)
        dds[s] = float(((run_max - curve) / run_max * 100.0).max())
        finals[s] = curve[-1]
    pct = lambda a, q: float(np.percentile(a, q))
    return {
        "n_sims": int(n_sims), "n_trades_per_sim": int(len(rets)),
        "final_equity": {"p5": pct(finals, 5), "p25": pct(finals, 25), "median": pct(finals, 50),
                         "p75": pct(finals, 75), "p95": pct(finals, 95)},
        "max_drawdown_pct": {"p5": pct(dds, 5), "p50": pct(dds, 50), "p95": pct(dds, 95)},
        "probability_of_loss": float((finals < initial_equity).mean() * 100.0),
    }
