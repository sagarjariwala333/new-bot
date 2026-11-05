"""
chart_api.py
============

CHART tab (owner request 2026-10-24): the REAL price candles of the pair on top and the SHADOW tracker (the paper
equity of the shadow) below, on the same time line, to see if the shadow follows the real price and trend.
Read-only: it only reads candles from the exchange and the bot's own files. It never places, changes or closes
anything.

On the candles: real trade entries / exits (from the ledger), shadow trade entries / exits (from the shadow), and
the open real trade's Stop Loss, Trailing Stop, Max loss and take-profit levels (from the hub memory).
"""

from __future__ import annotations

import math

from app import hub_state, ledger
from app import shadow_book
from app import strategy_hub as hub

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000,
    "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000, "1M": 2_592_000_000,
}
MAX_CANDLES = 1000


class ChartError(ValueError):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _pair_and_client(platform: str, account_id: str, symbol: str):
    """(pair config, client, owns_client). The running instance's client is used when there is one; otherwise a
    short-lived client is made for this read and closed again."""
    if platform == "okx":
        from app.okx_store import okx_store
        from app.okx_manager import okx_manager
        from app.okx_futures import OKXFuturesClient
        acc = okx_store.get_account(account_id)
        if acc is None or symbol not in acc.pairs:
            raise ChartError(404, "pair not found")
        inst = okx_manager.get(account_id, symbol)
        if inst is not None and inst._task is not None and not inst._task.done():
            return acc.pairs[symbol], inst.client, False
        k, s, ph = okx_store.get_credentials(account_id)
        return acc.pairs[symbol], OKXFuturesClient(api_key=k, api_secret=s, passphrase=ph, demo=acc.demo), True
    if platform == "binance":
        from app.store import store
        from app.manager import manager
        from app.binance_futures import BinanceFuturesClient
        acc = store.get_account(account_id)
        if acc is None or symbol not in acc.pairs:
            raise ChartError(404, "pair not found")
        inst = manager.get(account_id, symbol)
        if inst is not None and inst._task is not None and not inst._task.done():
            return acc.pairs[symbol], inst.client, False
        k, s = store.get_credentials(account_id)
        return acc.pairs[symbol], BinanceFuturesClient(api_key=k, api_secret=s, testnet=acc.testnet,
                                                       account_id=account_id), True
    raise ChartError(400, "platform must be binance or okx")


def _real_markers(account_id: str, symbol: str, t_from_ms: int) -> list[dict]:
    out = []
    seen_entries = set()
    for r in ledger.list_trades(account_id, symbol):
        try:
            opened_ms, closed_ms = int(float(r.opened_at) * 1000), int(float(r.closed_at) * 1000)
        except (TypeError, ValueError):
            continue
        if closed_ms < t_from_ms:
            continue
        key = (opened_ms, r.direction)
        if key not in seen_entries and opened_ms >= t_from_ms:      # one entry marker per trade (TP rows share it)
            seen_entries.add(key)
            out.append({"kind": "real_entry", "t": opened_ms, "price": r.entry_price, "dir": r.direction})
        out.append({"kind": "real_exit", "t": closed_ms, "price": r.exit_price, "dir": r.direction,
                    "reason": r.reason, "pnl": r.pnl})
    return out


def _shadow_markers(book: dict | None, t_from_ms: int) -> list[dict]:
    out = []
    if not book:
        return out
    rows = list(book.get("trades") or [])
    if book.get("current"):
        rows.append(dict(book["current"], exit_time=None))
    for r in rows:
        et = r.get("entry_time")
        if et is not None and et >= t_from_ms:
            out.append({"kind": "shadow_entry", "t": int(et), "price": r.get("signal_close"), "dir": r.get("direction")})
        xt = r.get("exit_time")
        if xt is not None and xt >= t_from_ms:
            out.append({"kind": "shadow_exit", "t": int(xt), "price": r.get("exit_price"), "dir": r.get("direction"),
                        "reason": r.get("exit_reason"), "result_pct": r.get("result_pct")})
    return out


def _levels(hs) -> list[dict]:
    """The open REAL trade's protective levels and take-profit targets (from the hub memory)."""
    out = []
    lad = hs.ladder or {}
    ex_name = (lad.get("ex") or {}).get("name")
    names = {"ml": "Max Loss per Trade", "sl": "Stop Loss", "ts": "Trailing Stop"}
    for k in ("ts", "sl", "ml"):
        p = lad.get(k)
        if p is not None:
            out.append({"kind": k, "label": names[k] + (" (on the exchange)" if k == ex_name else ""), "price": p})
    for lv in ((hs.tp or {}).get("levels") or []):
        if lv.get("on") and lv.get("state") in ("pending", "placed"):
            out.append({"kind": "tp", "label": f"TP{lv['index']}", "price": lv.get("price")})
    return out


SR_USED_BY = {
    "support": {"bounce_long": "Bounce long entry", "brk_short": "Breakout short entry",
                "bx_break": "Exit for longs: range breaks", "bx_other": "Exit for shorts: other end of the range",
                "stop_zone_on": "Stop for longs"},
    "resistance": {"bounce_short": "Bounce short entry", "brk_long": "Breakout long entry",
                   "bx_break": "Exit for shorts: range breaks", "bx_other": "Exit for longs: other end of the range",
                   "stop_zone_on": "Stop for shorts"},
}


def sr_payload(frame, plan: dict, candle_times: list[int]) -> dict:
    """What the Chart tab draws for Support / Resistance: one value per shown candle for each line (top, bottom,
    2nd top / bottom, middle, locked level), the entry signals, and the table of the levels now. Read-only."""
    sr = hub.sr_cfg(plan)
    if not sr or frame is None:
        return {"on": False}
    times = frame["open_time"].astype("int64").tolist()
    pos = {t: i for i, t in enumerate(times)}

    def series(col):
        vals = frame[col].tolist()
        out = []
        for t in candle_times:
            i = pos.get(int(t))
            v = vals[i] if i is not None else None
            out.append(None if v is None or not math.isfinite(v) else float(v))
        return out

    lines = {k: series("sr_" + k) for k in ("s1", "r1", "s2", "r2", "mid", "lock")}
    marks = []
    t0 = candle_times[0] if candle_times else 0
    for i in frame.index[(frame["sr_long"] | frame["sr_short"])]:
        t = int(times[i])
        if t < t0:
            continue
        marks.append({"t": t, "dir": "LONG" if bool(frame["sr_long"].iat[i]) else "SHORT",
                      "origin": {1: "Bounce", 2: "Breakout", 3: "Retest"}.get(int(frame["sr_origin"].iat[i]), ""),
                      "price": float(frame["close"].iat[i])})
    rows = []
    for r in frame.attrs.get("sr_levels", []):
        row = dict(r)
        f = row.pop("formed", None)
        row["formed_t"] = int(times[f]) if f is not None and 0 <= f < len(times) else None
        side = row["side"]
        if row["src"] == "lock":
            used = ["Retest long entry" if side == "support" else "Retest short entry"]
            status = "Waiting for the retest" + (" - new entries wait" if row.get("blocking") else "")
        else:
            used = []
            if row["nearest"]:
                used = [txt for key, txt in SR_USED_BY[side].items() if sr.get(key)]
            status = ("Nearest " if row["nearest"] else "Next ") + side
            if not used:
                used = ["Shown only"]
        row["used_by"] = used
        row["status"] = status
        rows.append(row)
    return {"on": True, "lines": lines, "markers": marks, "table": rows,
            "entry_ticked": hub.sr_entry_ticked(sr)}


async def chart_data(platform: str, account_id: str, symbol: str, limit: int = 300) -> dict:
    limit = max(10, min(int(limit or 300), MAX_CANDLES))
    pc, client, owns = _pair_and_client(platform, account_id, symbol)
    plan_for_sr = getattr(pc, "plan", None) or {}
    sr_on = bool((plan_for_sr.get("sr") or {}).get("on"))
    raw_sr = None
    try:
        raw = await client.get_klines(symbol, pc.timeframe, limit=limit)
        if sr_on:         # the same history the bot uses, so the lines are the ones it trades
            raw_sr = await client.get_klines(symbol, pc.timeframe, limit=hub.HUB_HISTORY_CANDLES + 1)
    finally:
        if owns:
            try:
                await client.close()
            except Exception:
                pass
    candles = []
    for row in raw:
        try:
            candles.append({"t": int(row[0]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                            "c": float(row[4])})
        except (TypeError, ValueError, IndexError):
            continue
    tf_ms = TF_MS.get(pc.timeframe, 3_600_000)
    t_from = candles[0]["t"] if candles else 0
    hs = hub_state.load(platform, account_id, symbol)
    book = hs.shadow_book
    plan = getattr(pc, "plan", None) or {}
    sr_out = {"on": False}
    if sr_on:
        try:
            import pandas as pd
            df = pd.DataFrame(raw_sr, columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                                               "qav", "trades", "tbbav", "tbqav", "ignore"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            df["open_time"] = df["open_time"].astype("int64")
            frame = hub.compute_hub(df.iloc[:-1].reset_index(drop=True), plan_for_sr)   # closed candles only
            sr_out = sr_payload(frame, hub.complete_plan(plan_for_sr), [c["t"] for c in candles])
        except Exception as e:           # a half-saved plan must not break the chart
            sr_out = {"on": True, "error": str(e)}
    return {
        "platform": platform, "symbol": symbol, "timeframe": pc.timeframe, "candle_ms": tf_ms,
        "candles": candles, "sr": sr_out,
        "real_markers": _real_markers(account_id, symbol, t_from),
        "shadow_markers": _shadow_markers(book, t_from),
        "levels": _levels(hs),
        "shadow_on": bool((plan.get("shadow") or {}).get("on")),
        "shadow_line": shadow_book.line_on_candles(book, [c["t"] for c in candles], tf_ms) if book else [],
        "shadow_start": (book or {}).get("start"),
        "shadow_equity": (book or {}).get("equity"),
    }
