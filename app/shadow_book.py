"""
shadow_book.py
==============

SHADOW (owner rule 2026-10-24): "Shadow should, as it says, shadow the real trade all the way - EXCEPT that it
should NOT go into pause when the actual trade goes to pause."

The shadow book is a PAPER copy of the pair's Strategy Hub plan that runs all the time while the Shadow mode box
is picked - also while real trading is paused (the trades-off period after a new high, Stop on the pair,
STOP ALL). It follows the whole plan exactly like a real trade:
  * at the candle close: entry rules, entry wait, combine, flip (close, flat, open the new side), hold rule, first trade on a
    fresh reversal, Allow Mid-Trend Re-entry, Lowest low / highest high and Moving average trailing levels;
  * on the live price (about every second): Stop Loss, Max loss, Step trailing, the Trailing Stop and the
    take-profit targets (partial closes; what they do not close keeps running).
It NEVER sends an order and never touches real money or the fixed-base running total. Its result is its own paper
equity line (starts at the Shadow box's "Tracker starting amount"), shown on the Chart tab under the real price.

Paper fills: a stop or a target fills at its own level; a flip closes at the candle close. Entry = candle close plus
the paper slippage. Fees are not modelled (same as before). Pure logic: no exchange calls, no I/O.
"""

from __future__ import annotations

import math

from app import strategy_hub as hub

MAX_TRADES = 500        # closed paper trades kept
MAX_CURVE = 5000        # equity points kept


def new_book(start_equity: float) -> dict:
    start = float(start_equity) if start_equity and start_equity > 0 else 1.0
    return {
        "start": start, "equity": start,
        "pos": 0, "entry": None, "signal_close": None, "alloc": None, "lev": None, "entry_time": None,
        "frac": 0.0,                      # share of the original size still open (1.0 = all)
        "levels": {"ml": None, "sl": None, "ts": None}, "best": None, "step": None,
        "tps": [], "legs": [],
        "wait": {"pend_dir": 0, "pend_bars": 0, "pend_from_flip": False},
        "reentry_block": 0, "reentry_forced": False, "first_done": False, "last_dir_e": 0,
        "current": None, "trades": [], "curve": [], "last_candle": None,
    }


def _point(book: dict, t_ms: int) -> None:
    curve = book["curve"]
    if curve and curve[-1][0] >= t_ms:
        curve[-1] = [curve[-1][0], book["equity"]]
    else:
        curve.append([int(t_ms), book["equity"]])
    if len(curve) > MAX_CURVE:
        del curve[: len(curve) - MAX_CURVE]


def _dir_name(pos: int) -> str:
    return "LONG" if pos == 1 else "SHORT"


def _close_part(book: dict, price: float, portion: float, reason: str, t_ms: int) -> None:
    """Close `portion` (share of the ORIGINAL size) at `price`; the result moves the paper equity."""
    portion = min(max(portion, 0.0), book["frac"])
    if portion <= 0:
        return
    entry = book["entry"]
    move = (price - entry) / entry if book["pos"] == 1 else (entry - price) / entry
    pct = move * (book["alloc"] / 100.0) * book["lev"] * portion * 100.0
    book["equity"] = book["equity"] * (1.0 + pct / 100.0)
    book["frac"] = max(book["frac"] - portion, 0.0)
    book["legs"].append({"reason": reason, "price": float(price), "portion": portion, "result_pct": pct,
                         "time": int(t_ms)})
    _point(book, t_ms)


def _finish(book: dict, t_ms: int, max_trades: int = MAX_TRADES) -> dict:
    """The trade is fully closed: one record for it, the side is blocked for re-entry (the plan decides if that
    matters), the book goes flat. Returns the record."""
    rec = dict(book["current"] or {})
    legs = book["legs"]
    rec["exit_time"] = int(t_ms)
    rec["exit_price"] = legs[-1]["price"] if legs else None
    rec["exit_reason"] = ", ".join(l["reason"] for l in legs) if legs else None
    rec["result_pct"] = sum(l["result_pct"] for l in legs) if legs else 0.0
    rec["legs"] = list(legs)
    rec["equity_after"] = book["equity"]
    book["trades"].append(rec)
    if len(book["trades"]) > max_trades:
        del book["trades"][: len(book["trades"]) - max_trades]
    book["reentry_block"] = book["pos"]
    book["reentry_forced"] = False          # "Exit when" -> wait for a fresh signal sets it again after this
    book.update(pos=0, entry=None, signal_close=None, alloc=None, lev=None, entry_time=None, frac=0.0,
                levels={"ml": None, "sl": None, "ts": None}, best=None, step=None, tps=[], legs=[], current=None,
                bars=0, be=None, sr_origin=0)
    return rec


def _close_all(book: dict, price: float, reason: str, t_ms: int) -> dict:
    _close_part(book, price, book["frac"], reason, t_ms)
    return _finish(book, t_ms)


def on_price(book: dict, price: float, plan: dict, t_ms: int) -> list[dict]:
    """The live price. Step trailing, take-profit targets (in order, partial), then the nearest stop level.
    Returns the paper trades that closed (fully) here."""
    closed: list[dict] = []
    if not book or book["pos"] == 0 or price is None or not math.isfinite(price) or price <= 0:
        return closed
    d = _dir_name(book["pos"])
    lv = book["levels"]
    # 1) Step trailing follows the best price
    if (d == "LONG" and price > book["best"]) or (d == "SHORT" and price < book["best"]):
        book["best"] = float(price)
    st = book.get("step")
    if st:
        lvl = hub.step_level(d, book["signal_close"], st["start"], st["gap"], book["best"],
                             st.get("begin"), st.get("first"))
        if lvl is not None and hub.is_tighter(d, lvl, lv.get("ts")):
            lv["ts"] = float(lvl)
    # 2) take profit: the next target first, then the next (each is placed only after the one before it is hit)
    on = [t for t in book["tps"] if t["on"]]
    last_on = on[-1] if on else None
    total_on = sum(t["pct"] for t in on)
    for t in on:
        if t["state"] != "pending":
            continue
        hit = price >= t["price"] if d == "LONG" else price <= t["price"]
        if not hit:
            break
        portion = book["frac"] if (t is last_on and total_on >= 99.999) else t["pct"] / 100.0
        _close_part(book, t["price"], portion, f"TP{t['index']}", t_ms)
        t["state"] = "filled"
        if book["frac"] <= 1e-9:
            closed.append(_finish(book, t_ms))
            return closed
    # 2b) BREAK-EVEN: after TP n / the After ATR move -> the Stop Loss goes to the entry price (only tightens)
    be = book.get("be")
    if hub.be_due(d, be, price, {t["index"] for t in book["tps"] if t["state"] == "filled"}):
        be["done"] = True
        if hub.is_tighter(d, book["entry"], lv.get("sl")):
            lv["sl"] = float(book["entry"])
    # 3) the nearest stop level (Trailing Stop, Stop Loss, Max loss): touched -> the rest closes at that level
    name, level = hub.nearest_level(d, lv)
    if name is not None:
        touched = price <= level if d == "LONG" else price >= level
        if touched:
            closed.append(_close_all(book, level, hub.LADDER_NAMES[name], t_ms))
    return closed


def on_candle(book: dict, snap, plan: dict, *, tick_size: float, last_total, balance, t_ms: int) -> tuple[list[str], list[dict]]:
    """One closed candle. Returns (log lines, paper trades that closed fully on this candle)."""
    events: list[str] = []
    closed: list[dict] = []
    if snap.open_time == book.get("last_candle"):
        return events, closed
    book["last_candle"] = snap.open_time
    reverse = plan["flip_action"] == hub.FLIP_ACTIONS[0]

    # the candle close is also a price
    for rec in on_price(book, snap.close, plan, t_ms):
        closed.append(rec)
        events.append(f"paper {rec['direction']} closed ({rec['exit_reason']}) result {rec['result_pct']:+.3f}%")

    flipped_to = 0
    closing_dir = 0
    rule_close = False      # AUDIT FIX D: closed by the trailing level already broken / the time stop on this close
    if book["pos"] != 0:
        d = _dir_name(book["pos"])
        if hub.flip_decision(d, snap, book["signal_close"], plan) == "FLIP":
            flipped_to = snap.dir_f
            closing_dir = book["pos"]
            rec = _close_all(book, snap.close, "FLIP", t_ms)
            closed.append(rec)
            events.append(f"paper {d} closed by flip @ {snap.close} result {rec['result_pct']:+.3f}%")
        elif hub.exit_decision(d, snap, plan) == "EXIT":
            # EXIT WHEN: the MA(s) curve against the trade at this candle close
            closing_dir = book["pos"]
            rec = _close_all(book, snap.close, "Exit when", t_ms)
            closed.append(rec)
            if hub.exit_after(plan) == hub.EXIT_FLIP:
                flipped_to = -closing_dir
                events.append(f"paper {d} closed by Exit when @ {snap.close} result {rec['result_pct']:+.3f}% "
                              f"(then the other side)")
            else:
                closing_dir = 0
                book["reentry_forced"] = True       # flat until a fresh entry signal, even if re-entry is allowed
                rule_close = True
                events.append(f"paper {d} closed by Exit when @ {snap.close} result {rec['result_pct']:+.3f}% "
                              f"(flat until a fresh signal)")
        elif hub.sr_exit_decision(d, snap, plan, int(book.get("sr_origin") or 0)) == "EXIT":
            # SUPPORT / RESISTANCE exit: the candle close meets a ticked exit; flat until a fresh signal
            rec = _close_all(book, snap.close, "Support / Resistance exit", t_ms)
            closed.append(rec)
            book["reentry_forced"] = True
            rule_close = True
            events.append(f"paper {d} closed by a Support / Resistance exit @ {snap.close} result "
                          f"{rec['result_pct']:+.3f}% (flat until a fresh signal)")
        else:
            # Lowest low / highest high and Moving average trailing: a new level at the candle close
            tr = plan["trail"]
            if tr["on"] and tr["type"] in (hub.TRAIL_LLHH, hub.TRAIL_MA, hub.TRAIL_CANDLE, hub.TRAIL_SUP):
                if tr["type"] == hub.TRAIL_LLHH:
                    cand = snap.trail_low if d == "LONG" else snap.trail_high
                elif tr["type"] == hub.TRAIL_SUP:
                    cand = getattr(snap, "sup_line", float("nan"))
                elif tr["type"] == hub.TRAIL_CANDLE:
                    cand = hub.candle_trail_level(d, snap.low, snap.high, book.get("tc_dist"))
                    if (hub.candle_start_profit(plan) and book["levels"].get("ts") is None
                            and not hub.candle_armed(d, snap.close, book.get("tc_arm"))):
                        cand = None         # "Only once in profit": no candle has closed far enough yet
                else:
                    cand = snap.ma1 if tr["ma"] == "MA 1" else snap.ma2
                lv = book["levels"]
                if cand is not None and math.isfinite(cand):
                    _, in_charge = hub.nearest_level(d, lv)
                    if hub.is_tighter(d, cand, lv.get("ts")) and hub.is_tighter(d, cand, in_charge):
                        safe = cand < snap.close if d == "LONG" else cand > snap.close
                        if safe:
                            lv["ts"] = float(cand)
                        elif tr["type"] in (hub.TRAIL_LLHH, hub.TRAIL_CANDLE):
                            rec = _close_all(book, snap.close, "Trailing Stop", t_ms)
                            closed.append(rec)
                            rule_close = True
                            events.append(f"paper {d} closed: the trailing level {cand} was already broken")
            # TIME STOP: open N candles and not in profit -> close at the candle close
            if book["pos"] != 0:
                book["bars"] = int(book.get("bars") or 0) + 1
                if hub.time_stop_due(d, plan, book["bars"], snap.close, book["signal_close"]):
                    rec = _close_all(book, snap.close, "Time stop", t_ms)
                    closed.append(rec)
                    rule_close = True
                    events.append(f"paper {d} closed by time stop @ {snap.close} result {rec['result_pct']:+.3f}%")

    if rule_close:
        # AUDIT FIX D (owner 2026-10-26): like the live bot, a trade closed by the trailing level / the time stop at
        # this candle close is not followed by an entry on the same candle; the next candle is judged fresh.
        if snap.dir_e != 0:
            book["last_dir_e"] = snap.dir_e
        _point(book, t_ms)
        return events, closed

    # entry rules (same machine as the real trade)
    flat = book["pos"] == 0
    first_ok = True
    if plan["wait_first_reversal"] and not book["first_done"]:
        last = book.get("last_dir_e") or 0
        first_ok = snap.dir_e != 0 and last != 0 and snap.dir_e != last
    block = closing_dir or book.get("reentry_block") or 0
    if plan.get("allow_reentry") and not book.get("reentry_forced"):
        block = 0
        book["reentry_block"] = 0
    reentry_ok = True
    if block:
        if flat and not closing_dir and snap.dir_e != block:
            book["reentry_block"] = 0
            book["reentry_forced"] = False
        else:
            reentry_ok = snap.dir_e != block
    ws = hub.WaitState(**book["wait"])
    ready = hub.wait_step(ws, flipped_to=flipped_to, reverse=reverse, flat=flat, dir_e=snap.dir_e,
                          dir_f=snap.dir_f, first_entry_ok=first_ok and reentry_ok, entry_wait=plan["entry_wait"],
                          flip_wait=hub.flip_wait(plan))
    book["wait"] = {"pend_dir": ws.pend_dir, "pend_bars": ws.pend_bars, "pend_from_flip": ws.pend_from_flip}
    if snap.dir_e != 0:
        book["last_dir_e"] = snap.dir_e       # last non-zero entry signal (first-trade rule, next candle)

    if ready != 0 and book["pos"] == 0:
        direction = "LONG" if ready == 1 else "SHORT"
        size = hub.paper_size(direction, snap, plan, last_total, balance)
        if size is None and hub.counter_entry_off(hub.entry_size(direction, snap, plan)[2], plan):
            events.append(f"paper {direction} not opened: counter-trend entry and the counter-trend size is 0")
        elif size is None:
            events.append("paper entry skipped: the available balance could not be read")
        else:
            alloc, lev, margin = size
            close = snap.close
            ml = None
            if plan["sizing"]["stop_on"]:
                frac = hub.paper_stop_fraction(plan, alloc, lev, margin)
                if frac is not None:
                    ml = close * (1 - frac) if ready == 1 else close * (1 + frac)
            qty = (margin * lev / close) if (margin and close > 0) else None
            sl = hub.sl_level(direction, close, getattr(snap, "sl_atr", float("nan")), plan, snap=snap, qty=qty)
            if sl is None:
                events.append(f"paper {direction}: "
                              f"{hub.SL_NOT_WORKED_MSG if (plan.get('sl') or {}).get('on') else hub.SL_MISSING_MSG}")
            step = hub.step_setup(direction, close, getattr(snap, "ts_atr", float("nan")), plan, sl)
            # Trailing Stop "Signal candle high / low +/- ATR": first level from the signal candle (2026-10-28)
            tc_dist = hub.candle_trail_distance(plan, close, getattr(snap, "ts_atr", float("nan")))
            ts0 = hub.candle_trail_level(direction, snap.low, snap.high, tc_dist)
            if plan["trail"]["on"] and plan["trail"]["type"] == hub.TRAIL_CANDLE and ts0 is None:
                events.append(f"paper {direction}: the Trailing Stop (Signal candle +/- ATR) level could not be "
                              f"worked out (ATR not available yet)")
            tc_arm = None
            if ts0 is not None and hub.candle_start_profit(plan):
                tc_arm = hub.candle_arm_price(direction, close, getattr(snap, "ts_atr", float("nan")), plan)
                ts0 = None
            if plan["trail"]["on"] and plan["trail"]["type"] == hub.TRAIL_SUP:
                ts0 = hub.sup_trail_level(direction, getattr(snap, "sup_line", float("nan")), close)
            tps = []
            if plan["tp"]["on"]:
                atr = snap.tp_atr
                if plan["tp"].get("basis", hub.BASIS_ATR) != hub.BASIS_ATR or (math.isfinite(atr) and atr > 0):
                    for t in hub.tp_prices(direction, close, atr, plan):
                        if t["on"] and math.isfinite(t["price"]):
                            tps.append({"index": t["index"], "price": float(t["price"]), "pct": float(t["pct"]),
                                        "on": True, "state": "pending"})
            slip = plan["shadow"]["slippage_ticks"] * (tick_size or 0.0)
            book.update(pos=int(ready), signal_close=float(close),
                        entry=float(close + slip if ready == 1 else close - slip),
                        alloc=float(alloc), lev=float(lev), entry_time=int(snap.open_time), frac=1.0,
                        levels={"ml": ml, "sl": sl, "ts": ts0}, best=float(close), step=step, tps=tps, legs=[],
                        tc_dist=(float(tc_dist) if ((ts0 is not None or tc_arm is not None) and tc_dist is not None) else None),
                        tc_arm=(float(tc_arm) if tc_arm is not None else None),
                        first_done=True, bars=0,
                        sr_origin=(int(getattr(snap, "sr_origin", 0) or 0)
                                   if getattr(snap, "sr_long" if ready == 1 else "sr_short", False) else 0),
                        be=hub.be_setup(direction, close, getattr(snap, "be_atr", float("nan")), plan))
            first_stop = hub.nearest_level(direction, book["levels"])[1]
            book["current"] = {"direction": direction, "entry_time": int(snap.open_time),
                               "entry_price": book["entry"], "signal_close": float(close),
                               "stop_price": first_stop, "alloc_pct": float(alloc), "leverage": float(lev)}
            events.append(f"paper {direction} opened @ {close} ({alloc:g}% at {lev:g}x, "
                          f"stop {('%.8g' % first_stop) if first_stop is not None else 'none'})")
    _point(book, t_ms)
    return events, closed


def line_on_candles(book: dict, candle_open_times: list[int], candle_ms: int) -> list:
    """The paper equity at each candle's close, for the Chart tab (one point per candle, from the candle where the
    book started). Returns [[open_time_ms, equity], ...]."""
    curve = (book or {}).get("curve") or []
    if not curve:
        return []
    out = []
    j = -1
    for t in candle_open_times:
        close_t = t + candle_ms
        while j + 1 < len(curve) and curve[j + 1][0] <= close_t:
            j += 1
        if j >= 0:
            out.append([t, curve[j][1]])
    return out
