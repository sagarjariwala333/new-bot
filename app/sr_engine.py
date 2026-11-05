"""SUPPORT / RESISTANCE engine (owner 2026-11-05).

Pure numpy, no look-ahead. The levels used on bar i are the ones known when bar i-1 had closed ("t = i-1");
bar i is then judged against them at its own close. Everything is worked out on the NORMAL candles (real prices),
never on Heikin Ashi / Renko.

Levels (the chart decides, the owner only ticks where to look):
  * Highest high / lowest low of the last N candles (flat lines)
  * Swing points: a high (low) that is the highest (lowest) of N candles on each side. It is only known N candles
    later, so it is used from then on and never earlier.
  * Diagonal: a straight line through the last 2 (or 3, least squares) swing highs = a falling/rising resistance,
    through the last 2 / 3 swing lows = a support. Its value moves with every candle.
Every level above the last close is a RESISTANCE, every level at / below it a SUPPORT (a broken level changes role).
The nearest `count` on each side are used; S1 / R1 (the nearest) are the range.

Signals on bar i (levels as of t = i-1, zone = zone_atr_pct % of the ATR of bar t):
  BOUNCE   long : low reaches S1 (+ zone), the candle closes back above S1 and is bullish; short mirrored at R1.
           "edges only": the close must be within edge% of the range from that end.
  BREAKOUT long : the FIRST candle that closes above R1 (+ ATR% margin, + volume above its average); short mirrored.
  RETEST   after a confirmed break the broken line is LOCKED (value + slope fixed). Long: a later candle touches the
           locked line's zone from above, closes above it and is bullish; short mirrored. Used once.
  The wait for the retest ends (any ticked, whichever is first): the level fails (a close back through it), a new
  range forms (a level nearer to the price than the locked one), the price runs away (a close beyond the level by an
  ATR %), or nothing ends it.
  CONFLICT a bounce and a breakout in opposite directions on one candle: no entry on it, the broken level is locked
  and new bounce / breakout entries wait (flat) until the retest or the end of the wait. A retest is still allowed.
"""
from __future__ import annotations

import math

import numpy as np

ORIGIN_NONE, ORIGIN_BOUNCE, ORIGIN_BREAKOUT, ORIGIN_RETEST = 0, 1, 2, 3
ORIGIN_NAMES = {0: "", 1: "bounce", 2: "breakout", 3: "retest"}
_EPS = 1e-9


def _finite(x) -> bool:
    return x is not None and math.isfinite(x)


def swing_points(h: np.ndarray, l: np.ndarray, n_side: int):
    """Swing highs / lows: (index, price) lists in time order, plus their confirm index (index + n_side)."""
    n = len(h)
    hi, lo = [], []
    N = int(n_side)
    for j in range(N, n - N):
        wh, wl = h[j - N:j + N + 1], l[j - N:j + N + 1]
        if np.isnan(wh).any() or np.isnan(wl).any():
            continue
        if int(np.argmax(wh)) == N and wh[N] == wh.max():
            hi.append((j, float(h[j])))
        if int(np.argmin(wl)) == N and wl[N] == wl.min():
            lo.append((j, float(l[j])))
    return hi, lo


def _line_through(points, k: int):
    """(slope, intercept) of the line through the last k points (k=2 exact, k=3 least squares) or None."""
    if len(points) < k:
        return None
    pts = points[-k:]
    xs = np.array([p[0] for p in pts], dtype=float)
    ys = np.array([p[1] for p in pts], dtype=float)
    if k == 2:
        if xs[1] == xs[0]:
            return None
        s = (ys[1] - ys[0]) / (xs[1] - xs[0])
        return float(s), float(ys[1] - s * xs[1])
    s, b = np.polyfit(xs, ys, 1)
    return float(s), float(b)


class _Lv:
    """One level line: value(x) = icpt + slope * x."""
    __slots__ = ("slope", "icpt", "src", "key", "formed", "tag")

    def __init__(self, slope, icpt, src, key, formed, tag=""):
        self.slope, self.icpt, self.src, self.key, self.formed, self.tag = slope, icpt, src, key, formed, tag

    def at(self, x: int) -> float:
        return self.icpt + self.slope * x


class _Sw:
    """Swing points as arrays (time order) with the index where each becomes known (index + N)."""

    def __init__(self, h, l, n_side, use):
        if use:
            hi, lo = swing_points(h, l, n_side)
        else:
            hi, lo = [], []
        self.hi, self.lo = hi, lo
        self.hj = np.array([j for j, _ in hi], dtype=int)
        self.hp = np.array([p for _, p in hi], dtype=float)
        self.lj = np.array([j for j, _ in lo], dtype=int)
        self.lp = np.array([p for _, p in lo], dtype=float)
        self.hc = self.hj + int(n_side)
        self.lc = self.lj + int(n_side)
        self.diag_cache: dict = {}


def _diag(sw: _Sw, which: str, kk: int, k: int):
    key = (which, kk, k)
    if key not in sw.diag_cache:
        pts = (sw.hi if which == "H" else sw.lo)[:kk]
        sw.diag_cache[key] = _line_through(pts, k)
    return sw.diag_cache[key]


def _levels_at(t: int, ref: float, cfg: dict, h, l, sw: _Sw):
    """The nearest `count` supports and resistances known after bar t has closed (list of _Lv, nearest first)."""
    count = int(cfg["count"])
    keep_n = count * 3 + 3                    # slack for lines found twice
    cands: list[tuple[_Lv, str]] = []          # (line, forced side or "")
    N = int(cfg["hhll_n"])
    if cfg["find_hhll"] and t - N + 1 >= 0:
        wh, wl = h[t - N + 1:t + 1], l[t - N + 1:t + 1]
        if not (np.isnan(wh).any() or np.isnan(wl).any()):
            jh, jl = t - N + 1 + int(np.argmax(wh)), t - N + 1 + int(np.argmin(wl))
            cands.append((_Lv(0.0, float(wh.max()), "hhll", ("hhll", "H"), jh, "Highest high"), "R"))
            cands.append((_Lv(0.0, float(wl.min()), "hhll", ("hhll", "L"), jl, "Lowest low"), "S"))
    kh = int(np.searchsorted(sw.hc, t, side="right"))     # swings known after bar t
    kl = int(np.searchsorted(sw.lc, t, side="right"))
    if cfg["find_swing"] and (kh or kl):
        pr = np.concatenate([sw.hp[:kh], sw.lp[:kl]])
        ix = np.concatenate([sw.hj[:kh], sw.lj[:kl]])
        is_h = np.concatenate([np.ones(kh, dtype=bool), np.zeros(kl, dtype=bool)])
        for sel in (pr <= ref, pr > ref):
            idx = np.nonzero(sel)[0]
            if len(idx) == 0:
                continue
            d = np.abs(pr[idx] - ref)
            order = idx[np.argsort(d, kind="stable")[:keep_n]]
            for q in order:
                nm = "Swing high" if is_h[q] else "Swing low"
                cands.append((_Lv(0.0, float(pr[q]), "swing", ("swing", int(ix[q]), nm[-1]), int(ix[q]), nm), ""))
    if cfg["diagonal"]:
        k = int(cfg["diag_points"])
        for which, kk, nm in (("H", kh, "Diagonal (tops)"), ("L", kl, "Diagonal (bottoms)")):
            ln = _diag(sw, which, kk, k) if kk >= k else None
            if ln is not None:
                s_, b_ = ln
                last = (sw.hj if which == "H" else sw.lj)[kk - 1]
                cands.append((_Lv(s_, b_, "diagonal", ("diag", nm, int(last)), int(last), nm), ""))
    sup, res = [], []
    for lv, side in cands:
        v = lv.at(t)
        if not _finite(v) or v <= 0:
            continue
        if side == "R" or (side == "" and v > ref):
            res.append((v - ref, lv))
        elif side == "S" or (side == "" and v <= ref):
            sup.append((ref - v, lv))
    out = []
    for lst in (sup, res):
        lst.sort(key=lambda a: a[0])
        keep, seen = [], []
        for d, lv in lst:
            v = lv.at(t)
            if any(abs(v - s_) <= _EPS * max(abs(v), 1.0) and abs(lv.slope - sl) <= _EPS for s_, sl in seen):
                continue                    # the same line found twice (e.g. a swing that is also the highest high)
            seen.append((v, lv.slope))
            keep.append(lv)
            if len(keep) >= count:
                break
        out.append(keep)
    return out[0], out[1]          # supports, resistances (nearest first)


def compute(o, h, l, c, v, atr, cfg: dict) -> dict:
    """o,h,l,c,v: numpy arrays of the NORMAL candles, atr: ATR array (cfg['atr_length']) of the same candles.
    Returns the per-bar arrays and `levels_now` (the lines as of the last closed candle, for the table)."""
    n = len(c)
    z = lambda: np.zeros(n, dtype=bool)          # noqa: E731
    nanv = lambda: np.full(n, np.nan)            # noqa: E731
    R = {"long": z(), "short": z(), "origin": np.zeros(n, dtype=np.int8), "stop_long": nanv(), "stop_short": nanv(),
         "exit_bl": z(), "exit_bs": z(), "exit_kl": z(), "exit_ks": z(),
         "s1": nanv(), "s2": nanv(), "r1": nanv(), "r2": nanv(), "mid": nanv(), "lock": nanv(),
         "lock_dir": np.zeros(n, dtype=np.int8), "break_up": z(), "break_dn": z(), "conflict": z(),
         "levels_now": []}
    use_sw = bool(cfg["find_swing"] or cfg["diagonal"])
    sw = _Sw(h, l, int(cfg["swing_n"]) if use_sw else 1, use_sw)
    zone_pct = float(cfg["zone_atr_pct"]) / 100.0 if cfg["zone_on"] else 0.0
    stop_pct = float(cfg["stop_zone_atr_pct"]) / 100.0 if cfg["stop_zone_on"] else None
    vlen = int(cfg["brk_vol_len"])
    lock = None            # {"dir", "slope", "icpt", "idx", "block"}
    last_break = None      # the latest confirmed break, kept for the "closes back inside" exit
    for i in range(1, n):
        t = i - 1
        prev_break = last_break           # the exits of this bar use the break known BEFORE this bar
        ref = c[t]
        if not _finite(ref) or not all(_finite(x) for x in (o[i], h[i], l[i], c[i])):
            continue
        sup, res = _levels_at(t, ref, cfg, h, l, sw)
        S1 = sup[0].at(i) if sup else float("nan")
        R1 = res[0].at(i) if res else float("nan")
        if sup:
            R["s1"][i] = S1
        if res:
            R["r1"][i] = R1
        if len(sup) > 1:
            R["s2"][i] = sup[1].at(i)
        if len(res) > 1:
            R["r2"][i] = res[1].at(i)
        if sup and res:
            R["mid"][i] = (S1 + R1) / 2.0
        a = atr[t] if t < len(atr) else float("nan")
        a_ok = _finite(a)
        zw = zone_pct * a if (a_ok and cfg["zone_on"]) else (0.0 if not cfg["zone_on"] else float("nan"))
        zone_ok = _finite(zw)
        if lock is not None:
            R["lock"][i] = lock["icpt"] + lock["slope"] * i
            R["lock_dir"][i] = lock["dir"]
        # ---------------------------------------------------------------- raw signals of this bar
        bull, bear = c[i] > o[i], c[i] < o[i]
        b_long = b_short = False
        if sup and zone_ok and cfg["bounce_long"]:
            b_long = bool(l[i] <= S1 + zw and c[i] > S1 and bull)
        if res and zone_ok and cfg["bounce_short"]:
            b_short = bool(h[i] >= R1 - zw and c[i] < R1 and bear)
        if cfg["bounce_edges"] and (b_long or b_short):
            if not (sup and res) or R1 <= S1:
                b_long = b_short = False
            else:
                width = (R1 - S1) * float(cfg["bounce_edge_pct"]) / 100.0
                b_long = b_long and (c[i] - S1) <= width
                b_short = b_short and (R1 - c[i]) <= width
        # a confirmed break = the first close beyond the nearest level (margin and volume are checked on that candle)
        k_up = k_dn = False
        margin = 0.0
        if cfg["brk_close_on"]:
            margin = float(cfg["brk_close_atr_pct"]) / 100.0 * a if a_ok else float("nan")
        vol_ok = True
        if cfg["brk_vol_on"]:
            vol_ok = False
            if t - vlen + 1 >= 0:
                w = v[t - vlen + 1:t + 1]
                if not np.isnan(w).any() and _finite(v[i]):
                    vol_ok = bool(v[i] >= float(cfg["brk_vol_mult"]) * float(w.mean()))
        if _finite(margin) and vol_ok:
            if res:
                k_up = bool(c[i] > R1 + margin)
            if sup:
                k_dn = bool(c[i] < S1 - margin)
        R["break_up"][i], R["break_dn"][i] = k_up, k_dn
        brk_long = k_up and bool(cfg["brk_long"])
        brk_short = k_dn and bool(cfg["brk_short"])

        # ---------------------------------------------------------------- the wait for the retest (lock)
        rt_long = rt_short = False
        if lock is not None and i > lock["idx"]:
            lv = lock["icpt"] + lock["slope"] * i
            lv_t = lock["icpt"] + lock["slope"] * t
            up = lock["dir"] == 1
            ended = False
            if cfg["wait_newrange"]:
                # a level nearer to the price than the locked one has formed on the retest side
                near = sup if up else res
                if near and ((near[0].at(t) > lv_t + _EPS * abs(lv_t)) if up else (near[0].at(t) < lv_t - _EPS * abs(lv_t))):
                    ended = True
            if not ended and zone_ok:
                if up and cfg["retest_long"]:
                    rt_long = bool(l[i] <= lv + zw and c[i] > lv and bull)
                elif not up and cfg["retest_short"]:
                    rt_short = bool(h[i] >= lv - zw and c[i] < lv and bear)
            if not ended and not (rt_long or rt_short):
                if cfg["wait_fail"] and ((up and c[i] < lv) or (not up and c[i] > lv)):
                    ended = True
                elif cfg["wait_runaway"] and a_ok:
                    far = float(cfg["wait_runaway_atr_pct"]) / 100.0 * a
                    if (up and c[i] >= lv + far) or (not up and c[i] <= lv - far):
                        ended = True
            if ended:
                lock = None

        # ---------------------------------------------------------------- one signal per bar
        blocked = lock is not None and lock["block"]       # a conflict wait that is still running at this bar
        long_any = b_long or brk_long or rt_long
        short_any = b_short or brk_short or rt_short
        conflict = long_any and short_any
        origin, took_lock = ORIGIN_NONE, None
        if conflict:
            long_any = short_any = False
            R["conflict"][i] = True
        else:
            if blocked:                       # flat while the conflict wait runs; only a retest may enter
                b_long = b_short = brk_long = brk_short = False
                long_any, short_any = rt_long, rt_short
        stop_lvl = float("nan")
        if long_any or short_any:
            is_long = long_any
            if rt_long or rt_short:
                origin = ORIGIN_RETEST
            elif brk_long or brk_short:
                origin = ORIGIN_BREAKOUT
            else:
                origin = ORIGIN_BOUNCE
            R["long" if is_long else "short"][i] = True
            R["origin"][i] = origin
            if stop_pct is not None and a_ok and zone_ok:
                if origin == ORIGIN_RETEST and lock is not None:
                    base = lock["icpt"] + lock["slope"] * i
                elif origin == ORIGIN_BREAKOUT:
                    base = R1 if is_long else S1
                else:
                    base = S1 if is_long else R1
                if _finite(base):
                    stop_lvl = base - zw - stop_pct * a if is_long else base + zw + stop_pct * a
                    R["stop_long" if is_long else "stop_short"][i] = stop_lvl
            if origin == ORIGIN_RETEST:
                lock = None                   # the retest is used once

        # ---------------------------------------------------------------- a confirmed break locks its level
        if (k_up or k_dn) and not (k_up and k_dn):
            ln = res[0] if k_up else sup[0]
            new = {"dir": 1 if k_up else -1, "slope": ln.slope, "icpt": ln.icpt, "idx": i,
                   "block": bool(conflict)}
            # the owner's rules: a conflict waits flat; a plain break only remembers the level for the retest
            lock = new
            last_break = new
        elif conflict and lock is not None:
            lock["block"] = True

        # ---------------------------------------------------------------- exits (judged at this candle close)
        if sup or res:
            if cfg["bx_other"]:
                R["exit_bl"][i] |= bool(res) and bool(h[i] >= R1)
                R["exit_bs"][i] |= bool(sup) and bool(l[i] <= S1)
            if cfg["bx_mid"] and sup and res:
                m = (S1 + R1) / 2.0
                R["exit_bl"][i] |= bool(h[i] >= m)
                R["exit_bs"][i] |= bool(l[i] <= m)
            if cfg["bx_break"]:
                R["exit_bl"][i] |= bool(sup) and bool(c[i] < S1)
                R["exit_bs"][i] |= bool(res) and bool(c[i] > R1)
        if cfg["brk_exit_inside"] and prev_break is not None:
            lvb = prev_break["icpt"] + prev_break["slope"] * i
            R["exit_kl"][i] = bool(prev_break["dir"] == 1 and c[i] < lvb)
            R["exit_ks"][i] = bool(prev_break["dir"] == -1 and c[i] > lvb)

    # ---------------------------------------------------------------- the levels now (for the table)
    if n >= 2:
        t = n - 1
        x = n                                   # the candle that is forming now
        sup, res = _levels_at(t, c[t], cfg, h, l, sw)
        a = atr[t] if t < len(atr) else float("nan")
        zw = (zone_pct * a) if (cfg["zone_on"] and _finite(a)) else 0.0
        rows = []
        for side, lst in (("Support", sup), ("Resistance", res)):
            for k, lv in enumerate(lst, 1):
                price = lv.at(x)
                rows.append({"name": f"{side} {k}", "side": side.lower(), "price": float(price),
                             "zone_from": float(price - zw), "zone_to": float(price + zw),
                             "distance": float(price - c[t]), "distance_pct": float((price - c[t]) / c[t] * 100.0)
                             if c[t] else float("nan"),
                             "how": lv.tag, "src": lv.src, "formed": int(lv.formed), "slope": float(lv.slope),
                             "diagonal": lv.src == "diagonal", "nearest": k == 1})
        if lock is not None:
            lvv = lock["icpt"] + lock["slope"] * x
            rows.append({"name": "Locked level", "side": "support" if lock["dir"] == 1 else "resistance",
                         "price": float(lvv), "zone_from": float(lvv - zw), "zone_to": float(lvv + zw),
                         "distance": float(lvv - c[t]),
                         "distance_pct": float((lvv - c[t]) / c[t] * 100.0) if c[t] else float("nan"),
                         "how": "Broken level (waiting for the retest)", "src": "lock", "formed": int(lock["idx"]),
                         "slope": float(lock["slope"]), "diagonal": lock["slope"] != 0.0, "nearest": False,
                         "blocking": bool(lock["block"])})
        R["levels_now"] = rows
    return R
