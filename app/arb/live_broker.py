"""LIVE broker: places REAL orders. Used only when a module's mode is "live" (Step 3).

Order type, price buffer, waiting time and sequence come from the tab's "orders" settings.
Extra safety compared with the standalone bot (owner decision, same account as the hub):
  * a futures leg is refused on any coin the hub trades on that account (rules.skip)
  * a futures leg is refused if the exchange already shows a position in that symbol (never nets into someone else's)
  * futures are ISOLATED margin only, and the margin the arbitrage uses is capped per exchange (rules.margin_cap_usdt)
"""
from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.arb.dec import D, floor_to, lcm_step
from app.arb.fills import Fill
from app.arb.live_http import ApiError, definitely_rejected


def wallet_key(ex, market):
    # OKX keeps spot and swap in ONE trading account; Binance keeps them apart.
    return (ex, "all") if ex == "okx" else (ex, market)


class LiveRules:
    """What the broker must respect besides the exchange's own rules. Filled in by the trader."""

    def __init__(self, skip=None, margin_used=None, margin_cap_usdt=0.0):
        self.skip = skip or (lambda ex, base: None)                 # -> reason text or None
        self.margin_used = margin_used or (lambda ex: 0.0)          # USDT margin the arbitrage already uses on ex
        self.margin_cap_usdt = float(margin_cap_usdt or 0.0)


class LiveBroker:
    mode = "live"

    def __init__(self, clients, fee_fn, orders_cfg, leverage, log, rules=None, bal_seconds=10, sleep=time.sleep,
                 clock=time.time, alert=None):
        self.clients = clients              # {(ex, market): client}
        self.fee_fn, self.cfg, self.lev, self.log = fee_fn, orders_cfg, int(leverage), log
        self.rules = rules or LiveRules()
        self.bal_seconds, self.sleep, self.clock = bal_seconds, sleep, clock
        self.alert = alert or (lambda text: None)
        self.bal, self.bal_time = {}, {}
        self.pos, self.pos_time = {}, {}
        self.futures_ready = set()

    # ---- balances ----
    def _client(self, ex, market):
        return self.clients.get((ex, market)) or next((c for (e, m), c in self.clients.items() if e == ex), None)

    def refresh(self, ex, market, force=False):
        wk = wallet_key(ex, market)
        if not force and self.clock() - self.bal_time.get(wk, 0) < self.bal_seconds:
            return
        cl = self._client(ex, market)
        try:
            raw = cl.get_balances(None)
            self.bal[wk] = {a: v["available"] for a, v in raw.items()}
            self.bal_time[wk] = self.clock()
        except ApiError as e:
            self.log.warning("Balance check failed on %s: %s", cl.tag if cl else ex, e)
            self.bal.pop(wk, None)

    def avail(self, ex, market, asset):
        self.refresh(ex, market)
        b = self.bal.get(wallet_key(ex, market))
        return None if b is None else b.get(asset, 0.0)

    def positions(self, ex, force=False):
        """{symbol: signed qty} of every open futures position on the exchange account, or None if unreadable."""
        if not force and self.clock() - self.pos_time.get(ex, 0) < self.bal_seconds:
            return self.pos.get(ex)
        cl = self.clients.get((ex, "futures"))
        if cl is None:
            return {}
        try:
            self.pos[ex] = cl.open_positions()
            self.pos_time[ex] = self.clock()
        except ApiError as e:
            self.log.warning("Position check failed on %s: %s", cl.tag, e)
            self.pos[ex] = None
        return self.pos[ex]

    def credit(self, ex, market, amount):
        pass    # live: the exchange moves the money itself

    def sellable(self, ex, market, base):
        return self.avail(ex, market, base) if market == "spot" else None

    def can_do_all(self, legs):
        need = {}
        new_margin = {}
        for l in legs:
            fee = self.fee_fn(l.ex, l.market)
            if l.market == "spot":
                if l.side == "buy":
                    k, a, amt = (l.ex, l.market), (l.ins.quote if l.ins else "USDT"), l.qty * l.ref_px * (1 + fee) * 1.002
                else:
                    k, a, amt = (l.ex, l.market), l.base, l.qty
            elif l.closing:
                continue
            else:
                why = self.rules.skip(l.ex, l.base)
                if why:
                    return False, why
                ps = self.positions(l.ex)
                if ps is None:
                    return False, "cannot read the open positions on %s" % l.ex
                if l.ins.symbol in ps:
                    return False, "%s already has a position in %s (not opened by the arbitrage)" % (l.ex, l.ins.symbol)
                margin = l.qty * l.ref_px / self.lev
                new_margin[l.ex] = new_margin.get(l.ex, 0.0) + margin
                k, a, amt = (l.ex, l.market), "USDT", margin * 1.1 + l.qty * l.ref_px * fee
            wk = wallet_key(*k)
            need[(wk, a, k)] = need.get((wk, a, k), 0.0) + amt
        cap = self.rules.margin_cap_usdt
        for ex, extra in new_margin.items():
            used = self.rules.margin_used(ex)
            if cap <= 0:
                return False, "no margin cap is set (risk.margin_cap_usdt)"
            if used + extra > cap + 1e-9:
                return False, "margin cap on %s: using %.2f + new %.2f > cap %.2f USDT" % (ex, used, extra, cap)
        for (wk, a, k), amt in need.items():
            have = self.avail(k[0], k[1], a)
            if have is None:
                return False, "cannot read %s balance on %s" % (a, k[0])
            if have + 1e-12 < amt:
                return False, "not enough %s on %s %s (have %.8g, need %.8g)" % (a, k[0], k[1], have, amt)
        return True, ""

    # ---- one order ----
    def _get(self, cl, ins, side, o=None, cl_id=None):
        last = None
        for _ in range(3):
            try:
                return cl.get_order(ins, side, order_id=o["id"] if o else None, cl_id=None if o else cl_id)
            except ApiError as e:
                last = e
                self.sleep(0.2)
        raise last

    def _settle(self, cl, ins, side, o, otype):
        poll = float(self.cfg.get("poll_ms", 300)) / 1000.0
        wait = float(self.cfg.get("wait_seconds", 3)) if otype == "limit_wait" else max(2.0, 10 * poll)
        t0 = self.clock()
        while o["open"] and self.clock() - t0 < wait:
            self.sleep(poll)
            o = self._get(cl, ins, side, o)
        if o["open"]:
            try:
                cl.cancel(ins, o["id"])
            except ApiError as e:
                self.log.warning("Cancel failed (%s %s): %s", cl.tag, ins.symbol, e)
            o = self._get(cl, ins, side, o)
        return o

    def _to_fill(self, o, leg, qty_wanted):
        taker = self.fee_fn(leg.ex, leg.market)
        filled = o["filled"]
        px = o["avg"] or leg.ref_px
        net = o["net"]
        if net is None:
            net = filled * (1 - taker) if (leg.market == "spot" and leg.side == "buy") else filled
        if filled <= 0:
            return Fill("none", order_id=o["id"])
        status = "filled" if filled >= qty_wanted * 0.999 else "partial"
        return Fill(status, filled, px, filled * px * taker, o["id"], net_qty=net)

    def _prepare_futures(self, cl, ins):
        key = (cl.name, ins.symbol)
        if key in self.futures_ready:
            return
        cl.set_isolated(ins)                      # isolated margin only
        cl.set_leverage(ins, self.lev)
        self.futures_ready.add(key)

    def _do_leg(self, leg, qty=None):
        cl = self.clients[(leg.ex, leg.market)]
        ins = leg.ins
        qty = ins.round_qty(D(leg.qty if qty is None else qty))
        if qty <= 0 or qty < ins.min_qty:
            return Fill("none", error="too small after rounding (%s)" % qty)
        otype = self.cfg.get("exit_type", "market") if leg.closing else self.cfg.get("type", "limit_ioc")
        price = None
        if otype != "market":
            buf = float(self.cfg.get("price_buffer_pct", 0)) / 100.0
            raw = leg.ref_px * (1 + buf if leg.side == "buy" else 1 - buf)
            price = ins.round_price(raw, up=(leg.side == "buy"))
        cl_id = "a" + uuid.uuid4().hex[:24]
        try:
            if leg.market == "futures" and not leg.closing:
                try:
                    self._prepare_futures(cl, ins)
                except ApiError as e:
                    return Fill("none", error="could not set isolated margin / leverage on %s %s: %s"
                                % (cl.tag, ins.symbol, str(e)[:160]))
            try:
                o = cl.place(ins, leg.side, qty, otype, price=price, tif="IOC" if otype == "limit_ioc" else "GTC",
                             reduce_only=leg.closing, cl_id=cl_id)
            except ApiError as e:
                if definitely_rejected(e):
                    return Fill("none", error=str(e)[:200])
                self.log.warning("Order reply unclear on %s (%s). Looking it up...", cl.tag, str(e)[:120])
                try:
                    o = self._get(cl, ins, leg.side, None, cl_id)
                except ApiError:
                    msg = "UNCERTAIN order on %s %s %s: %s" % (cl.tag, leg.side, ins.symbol, str(e)[:120])
                    self.alert("ARBITRAGE: " + msg + " - check the exchange by hand")
                    return Fill("error", error=msg)
            o = self._settle(cl, ins, leg.side, o, otype)
            return self._to_fill(o, leg, float(qty))
        except ApiError as e:
            return Fill("error", error="%s %s %s: %s" % (cl.tag, leg.side, ins.symbol, str(e)[:200]))
        except Exception as e:      # never let a bug leave the other leg unhandled
            return Fill("error", error="unexpected %s: %s" % (type(e).__name__, e))

    # ---- one or two legs ----
    def exec_legs(self, legs):
        try:
            if len(legs) == 1:
                return [self._do_leg(legs[0])]
            if self.cfg.get("sequence", "simultaneous") == "simultaneous":
                with ThreadPoolExecutor(max_workers=len(legs)) as pool:
                    return list(pool.map(self._do_leg, legs))
            # harder side first: the leg whose best-price size is smallest vs what we need
            order = sorted(range(len(legs)), key=lambda i: legs[i].top / max(legs[i].qty, 1e-18))
            first, second = order[0], order[1]
            res = [None, None]
            res[first] = self._do_leg(legs[first])
            if not res[first].got:
                res[second] = Fill("none", error="skipped: first leg did not fill")
            else:
                step = lcm_step(legs[second].ins.qty_step, legs[first].ins.qty_step)
                q2 = floor_to(res[first].net_qty, step)    # net_qty == qty except spot buys taxed in coin
                q2 = min(q2, D(legs[second].qty))
                res[second] = self._do_leg(legs[second], q2)
            return res
        finally:
            for l in legs:
                self.refresh(l.ex, l.market, force=True)
                if l.market == "futures":
                    self.pos_time[l.ex] = 0          # positions changed: read them again next time
