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

from app import hub_state, ledger
from app import shadow_book

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


async def chart_data(platform: str, account_id: str, symbol: str, limit: int = 300) -> dict:
    limit = max(10, min(int(limit or 300), MAX_CANDLES))
    pc, client, owns = _pair_and_client(platform, account_id, symbol)
    try:
        raw = await client.get_klines(symbol, pc.timeframe, limit=limit)
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
    return {
        "platform": platform, "symbol": symbol, "timeframe": pc.timeframe, "candle_ms": tf_ms,
        "candles": candles,
        "real_markers": _real_markers(account_id, symbol, t_from),
        "shadow_markers": _shadow_markers(book, t_from),
        "levels": _levels(hs),
        "shadow_on": bool((plan.get("shadow") or {}).get("on")),
        "shadow_line": shadow_book.line_on_candles(book, [c["t"] for c in candles], tf_ms) if book else [],
        "shadow_start": (book or {}).get("start"),
        "shadow_equity": (book or {}).get("equity"),
    }
