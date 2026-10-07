"""
analysis.py
===========

Offline analysis tools that run against HISTORICAL data only, never against
live capital, and never feed back into any live trading decision. Built
entirely on top of the BASE V3 functions in strategy.py (compute_indicators,
stop_price, in_position_action, shadow_step, position_qty) - this file adds no new trading rules of its own; it just
replays the exact same rules bar-by-bar over history and reports the
results.

Two entry points:
  - walk_forward_analysis(): splits history into sequential train/test
    folds and reports per-fold performance, so you can see whether results
    hold up out-of-sample rather than just on one lucky window.
  - monte_carlo_simulation(): resamples a list of realized trade PnLs (with
    replacement) many times to show a distribution of plausible equity
    curves / drawdowns, not just the single one history happened to produce.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app import strategy as strat


@dataclass
class SimTrade:
    direction: str
    entry_price: float       # signal price, before slippage
    exit_price: float        # signal price, before slippage
    filled_entry_price: float
    filled_exit_price: float
    gross_pnl: float         # price-only PnL, no costs
    pnl: float                # NET PnL after commission + slippage - what you'd actually keep
    costs: float              # commission + slippage, in quote-currency terms
    reason: str
    entry_idx: int
    exit_idx: int


def simulate_trades(df: pd.DataFrame, p: strat.StrategyParams, initial_equity: float,
                    commission_pct: float, slippage_pct: float) -> list[SimTrade]:
    """
    BASE V3 bar-by-bar replay over closed history (offline only - never
    used by a live decision). Same functions the live bot uses
    (compute_indicators, stop_price, in_position_action, shadow_step), in
    the Pine script's bar order:

      1. the fixed stop fills intrabar (from the bar AFTER entry)
      2. shadow-mode activation for a winning trade that set a new closed-
         equity high (a trade closed by a flip on bar i-1 is "seen" on bar
         i, like Pine with process_orders_on_close; a stop fill is seen on
         the same bar)
      3. shadow (paper) step
      4. flat -> entry at close if signal and real entries allowed;
         in position -> Hold Rule / close at close on an opposite signal
         (the new side can only open on a later bar)

    Costs: commission_pct per side and slippage_pct as a % of price per fill.
    There are no default cost values - they are entered on the dashboard.
    Returns only REAL trades - paper trades are not
    trades.
    """
    ind = strat.compute_indicators(df, p)
    trades: list[SimTrade] = []
    equity = initial_equity
    peak = initial_equity
    shadow = strat.ShadowState()
    position = None
    pending_close = None   # a trade closed by flip on the previous bar (seen now)

    def _book(pos, exit_price, reason, i):
        nonlocal equity
        direction = pos["direction"]
        filled_exit = exit_price * (1 - slippage_pct / 100) if direction == "LONG" \
            else exit_price * (1 + slippage_pct / 100)
        sign = 1 if direction == "LONG" else -1
        gross = sign * (filled_exit - pos["filled_entry_price"]) * pos["qty"]
        costs = (pos["qty"] * pos["filled_entry_price"] + pos["qty"] * filled_exit) * (commission_pct / 100)
        net = gross - costs
        equity += net
        t = SimTrade(direction=direction, entry_price=pos["entry_price"], exit_price=exit_price,
                     filled_entry_price=pos["filled_entry_price"], filled_exit_price=filled_exit,
                     gross_pnl=gross, pnl=net, costs=costs, reason=reason,
                     entry_idx=pos["entry_idx"], exit_idx=i)
        trades.append(t)
        return t

    def _maybe_activate(t):
        nonlocal peak
        if equity > peak:
            peak = equity
            if p.use_shadow and t.pnl > 0:
                strat.shadow_activate(shadow)

    for i in range(1, len(ind)):
        snap = strat.snapshot_at(ind, i)
        if any(pd.isna(x) for x in (snap.ema_sizing, snap.hma_prev, snap.adx, snap.atr_sc)):
            continue

        # 1) fixed stop, intrabar
        stopped = None
        if position is not None and i > position["entry_idx"]:
            st = position["stop"]
            if position["direction"] == "LONG" and snap.low <= st:
                # gap through the stop -> filled at the open (TradingView behaviour)
                stopped = _book(position, min(st, snap.open), "SL", i)
                position = None
            elif position["direction"] == "SHORT" and snap.high >= st:
                stopped = _book(position, max(st, snap.open), "SL", i)
                position = None

        # 2) activation (flip closed last bar, or stop filled this bar)
        if pending_close is not None:
            _maybe_activate(pending_close)
            pending_close = None
        if stopped is not None:
            _maybe_activate(stopped)

        # 3) shadow step (tick size unknown offline -> slippage 0 on paper entry)
        strat.shadow_step(shadow, snap, p, 0.0)

        # 4) real orders at bar close
        if position is None:
            if strat.real_entry_allowed(shadow, snap.open_time):
                direction = strat.entry_direction(snap)
                if direction:
                    alloc = strat.allocation_pct(direction, snap)
                    qty = strat.position_qty(equity, alloc, strat.trade_leverage(direction, snap, p), snap.close)
                    if qty > 0:
                        filled = snap.close * (1 + slippage_pct / 100) if direction == "LONG" \
                            else snap.close * (1 - slippage_pct / 100)
                        position = {
                            "direction": direction, "entry_price": snap.close, "filled_entry_price": filled,
                            "qty": qty, "entry_idx": i,
                            "stop": strat.stop_price(direction, snap.close, equity, qty, p),
                        }
            continue

        action = strat.in_position_action(position["direction"], snap, position["entry_price"], p)
        if action == "CLOSE":
            pending_close = _book(position, snap.close, "signal_flip", i)
            position = None

    return trades


def _fold_stats(trades: list[SimTrade], initial_equity: float) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": None, "total_pnl": 0.0, "total_costs": 0.0, "max_drawdown_pct": None}
    pnls = [t.pnl for t in trades]           # net of commission + slippage
    costs = [t.costs for t in trades]
    wins = [x for x in pnls if x > 0]
    equity_curve = np.cumsum(pnls) + initial_equity
    running_max = np.maximum.accumulate(equity_curve)
    drawdown_pct = ((running_max - equity_curve) / running_max * 100)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl": float(sum(pnls)),          # net
        "total_costs": float(sum(costs)),        # commission + slippage paid, for transparency
        "max_drawdown_pct": float(drawdown_pct.max()),
    }


def walk_forward_analysis(df: pd.DataFrame, p: strat.StrategyParams, n_folds: int,
                           initial_equity: float,
                           commission_pct: float, slippage_pct: float) -> dict:
    """Splits closed history into n_folds sequential, non-overlapping windows
    (each "fold" plays the role of an out-of-sample test window) and reports
    per-fold stats using the exact same rules throughout - no re-fitting of
    parameters happens here, this only checks consistency across time.
    commission_pct/slippage_pct are entered by the owner (see simulate_trades)."""
    n = len(df)
    fold_size = n // n_folds
    warmup = max(p.ema_sizing_length, p.hma_length, p.adx_di_length + p.adx_smoothing) + 50
    if fold_size < warmup:
        raise ValueError(
            f"Not enough history for {n_folds} folds - need at least "
            f"{warmup * n_folds} candles, have {n}."
        )
    folds = []
    for f in range(n_folds):
        start = f * fold_size
        end = n if f == n_folds - 1 else (f + 1) * fold_size
        window = df.iloc[start:end].reset_index(drop=True)
        trades = simulate_trades(window, p, initial_equity, commission_pct=commission_pct, slippage_pct=slippage_pct)
        folds.append({"fold": f + 1, "candles": len(window), **_fold_stats(trades, initial_equity)})
    return {"folds": folds, "n_folds": n_folds}


def monte_carlo_simulation(trade_pnls: list[float], n_sims: int,
                            initial_equity: float, seed: int | None = None) -> dict:
    """Resamples the given trade PnL sequence WITH replacement, n_sims times,
    to show a distribution of plausible outcomes rather than the single
    equity curve history happened to produce. Purely statistical - does not
    re-run the strategy or touch its formulas."""
    if not trade_pnls:
        raise ValueError("No trades to resample - run simulate_trades()/walk_forward first.")
    rng = np.random.default_rng(seed)
    n_trades = len(trade_pnls)
    pnls = np.array(trade_pnls)

    finals = np.empty(n_sims)
    max_drawdowns = np.empty(n_sims)
    for s in range(n_sims):
        sample = rng.choice(pnls, size=n_trades, replace=True)
        curve = np.cumsum(sample) + initial_equity
        running_max = np.maximum.accumulate(curve)
        dd = ((running_max - curve) / running_max * 100)
        finals[s] = curve[-1]
        max_drawdowns[s] = dd.max()

    def pct(arr, q):
        return float(np.percentile(arr, q))

    return {
        "n_sims": n_sims,
        "n_trades_per_sim": n_trades,
        "final_equity": {
            "p5": pct(finals, 5), "p25": pct(finals, 25), "median": pct(finals, 50),
            "p75": pct(finals, 75), "p95": pct(finals, 95),
        },
        "max_drawdown_pct": {
            "p5": pct(max_drawdowns, 5), "p50": pct(max_drawdowns, 50), "p95": pct(max_drawdowns, 95),
        },
        "probability_of_loss": float((finals < initial_equity).mean() * 100),
    }
