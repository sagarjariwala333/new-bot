"""Candle history cache (owner 2026-11-11): cuts the request load a lot.

The bot used to download ~1000 candles for every pair on every poll. Closed candles never change, so now it
downloads the full history once, and afterwards only the newest few candles and merges them in.
The result is the SAME list the full download would give. Safety checks:
  * the newest download must overlap the stored history, and every overlapping CLOSED candle must be identical,
    otherwise the full history is downloaded again;
  * a full download is also repeated every FULL_REFRESH_SECONDS no matter what.
"""
from __future__ import annotations

import time

TAIL = 8                        # newest candles asked for on a normal poll
FULL_REFRESH_SECONDS = 6 * 3600


def _same(a: list, b: list) -> bool:
    return all(str(a[i]) == str(b[i]) for i in range(1, 5))


async def cached_klines(client, symbol: str, interval: str, limit: int) -> list:
    store = getattr(client, "_kline_cache", None)
    if store is None:
        store = {}
        try:
            client._kline_cache = store
        except Exception:
            return await client.get_klines(symbol, interval, limit=limit)
    key = (symbol, interval, int(limit))
    ent = store.get(key)
    now = time.monotonic()
    if ent is not None and limit > 3 * TAIL and now - ent["full_at"] < FULL_REFRESH_SECONDS and len(ent["rows"]) >= limit:
        try:
            tail = await client.get_klines(symbol, interval, limit=TAIL)
            merged = _merge(ent["rows"], tail, limit)
        except Exception:
            raise
        if merged is not None:
            ent["rows"] = merged
            return list(merged)
    rows = await client.get_klines(symbol, interval, limit=limit)
    store[key] = {"rows": list(rows), "full_at": now}
    return list(rows)


def _merge(old: list, tail: list, limit: int):
    """None = cannot merge safely (caller does a full download)."""
    if not tail or not old:
        return None
    by_time = {int(r[0]): r for r in old}
    first_new = int(tail[0][0])
    last_old = int(old[-1][0])
    if first_new > last_old:                       # gap: some candles missed
        return None
    overlap = [r for r in tail if int(r[0]) in by_time]
    if not overlap:
        return None
    # candles that were already CLOSED when we stored them (all but our last, then-forming, one) must match
    for r in overlap:
        if int(r[0]) < last_old and not _same(r, by_time[int(r[0])]):
            return None
    cut = int(tail[0][0])
    kept = [r for r in old if int(r[0]) < cut]
    merged = kept + list(tail)
    if len(merged) < min(limit, len(old)):
        return None
    return merged[-limit:]
