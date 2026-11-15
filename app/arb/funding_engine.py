"""Funding-rate arbitrage on ONE exchange: buy spot + short the perpetual, collect funding.

Only the positive-funding side is built (longs pay shorts). Spot and futures money must both be
on the exchange already (the bot does not move money between wallets).
"""
import logging

from app.arb.dec import D, floor_to, lcm_step
from app.arb.engine import Engine
from app.arb.fills import Leg

PERIOD_MS = 8 * 3600 * 1000


class Funding(Engine):
    module = "funding"

    def __init__(self, mode, cfg, ctx, make_broker):
        super().__init__("funding", mode, cfg, ctx, make_broker)

    # ---- opportunities ----
    def compute_edges(self):
        c, out = self.cfg, []
        for ex in self.ctx.exchanges:
            fund = self.ctx.funding.get(ex, {})
            for base in self.ctx.funding_universe.get(ex, []):
                f = fund.get(base)
                sp = self.ctx.book(ex, "spot", base)
                pf = self.ctx.book(ex, "futures", base)
                if not (f and sp and pf):
                    continue
                rate_pct = f["rate"] * 100
                fees_rt = 2 * (self.fee(ex, "spot") + self.fee(ex, "futures")) * 100
                basis = (pf["bid"] - sp["ask"]) / sp["ask"] * 100
                periods = fees_rt / rate_pct if rate_pct > 0 else float("inf")
                out.append({"base": base, "ex": ex, "rate": rate_pct, "basis": basis, "fees": fees_rt,
                            "periods": periods, "sp": sp, "pf": pf, "f": f,
                            "net": rate_pct * float(c.get("expected_periods", 3)) - fees_rt})
        out.sort(key=lambda e: e["rate"], reverse=True)
        return out

    def step(self, now):
        self.manage(now)
        edges = self.compute_edges()
        self.board = edges[:10]
        self.enter(edges, now)

    def enter(self, edges, now):
        if not self.entries_allowed():
            return
        c = self.cfg
        done = 0
        for e in edges:
            if e["rate"] < float(c["min_funding_pct"]):
                break
            if done >= int(c.get("max_entries_per_loop", 1)):
                break
            if len(self.pos) >= int(c.get("max_open_trades", 3)) or self.base_blocked(e["base"]):
                continue
            if e["periods"] > float(c["max_breakeven_periods"]) or e["basis"] < float(c["min_basis_pct"]):
                continue
            done += 1
            self.try_enter(e, now)

    def try_enter(self, e, now):
        ex, base, sp, pf = e["ex"], e["base"], e["sp"], e["pf"]
        isp, ifu = self.ctx.ins(ex, "spot", base), self.ctx.ins(ex, "futures", base)
        if not isp or not ifu:
            return
        mts = float(self.ctx.s["pairs"].get("min_top_size_usdt", 0))
        if min(sp["aq"], pf["bq"]) * sp["ask"] < mts:
            return
        size = self.ctx.risk.cap(float(self.cfg["trade_size_usdt"]), self.mode)
        qty = D(min(size / sp["ask"], sp["aq"], pf["bq"]))
        qty = floor_to(qty, lcm_step(isp.qty_step, ifu.qty_step))
        if not (isp.size_ok(qty, sp["ask"]) and ifu.size_ok(qty, pf["bid"])):
            return
        legs = [Leg(ex, "spot", base, "buy", float(qty), sp["ask"], isp, top=sp["aq"]),
                Leg(ex, "futures", base, "sell", float(qty), pf["bid"], ifu, top=pf["bq"])]
        ok, why = self.broker.can_do_all(legs)
        if not ok:
            self.ctx.note("cant-fund-%s" % base, logging.INFO, "[%s] %s: skipped: %s", self.name, base, why)
            return
        self.log.info("[%s] ENTER %s on %s: funding %.4f%%/period (break-even %.2f periods), basis %.3f%%",
                      self.name, base, ex, e["rate"], e["periods"], e["basis"])
        fs, ff = self.broker.exec_legs(legs)
        self.fills_problem([fs, ff])
        if not fs.got and not ff.got:
            return
        size_u = float(qty) * sp["ask"]
        if fs.got != ff.got:      # a naked spot-long or perp-short is a directional bet: always undo it
            f, leg, side = (fs, legs[0], "buy") if fs.got else (ff, legs[1], "sell")
            self.one_leg(base, size_u, side, f, leg, "other leg did not fill", action="close_filled")
            return
        q = min(fs.net_qty, ff.qty)
        adj, kept = 0.0, {}
        for f, leg, side, have in ((fs, legs[0], "buy", fs.net_qty), (ff, legs[1], "sell", ff.qty)):
            rq = leg.ins.round_qty(D(have - q)) if have - q > 0 else D(0)
            trimmed = 0.0
            if rq > 0 and rq >= leg.ins.min_qty:
                a, trimmed = self.trim(leg, side, f, float(rq))
                adj += a
            kept[side] = have - trimmed               # what is REALLY held on the exchange
        recs = [self.new_leg_rec(ex, "spot", base, "buy", kept["buy"], fs.px, fs.fee * kept["buy"] / fs.net_qty),
                self.new_leg_rec(ex, "futures", base, "sell", kept["sell"], ff.px, ff.fee * kept["sell"] / ff.qty)]
        p = self.new_pos("open", base, q * fs.px, recs, ex=ex, rate_last=e["f"]["rate"],
                         next_fund_ms=e["f"]["next_time_ms"])
        p["adj"] = adj
        self.save()

    # ---- holding / exit ----
    def manage(self, now):
        c = self.cfg
        now_ms = now * 1000
        for pos in list(self.pos):
            ex, base = pos["ex"], pos["base"]
            f = self.ctx.funding.get(ex, {}).get(base)
            fut = pos["legs"][1]
            fb = self.ctx.book(ex, "futures", base)
            # estimated funding payment (the exchange pays it by itself in live mode)
            if fb and now_ms >= pos["next_fund_ms"] and fut["qty"] > 0:
                rate = f["rate"] if f else pos["rate_last"]
                credit = fut["qty"] * (fb["bid"] + fb["ask"]) / 2 * rate
                pos["funding"] += credit
                self.broker.credit(ex, "futures", credit)
                pos["rate_last"] = rate
                nxt = f["next_time_ms"] if f else 0
                pos["next_fund_ms"] = nxt if nxt > pos["next_fund_ms"] else pos["next_fund_ms"] + PERIOD_MS
                self.log.info("[%s] funding %s %s: %+.4f USDT (rate %.4f%%)", self.name, ex, base,
                              credit, rate * 100)
                self.save()
            net = self.unreal(pos)
            reason = pos.get("closing_reason")
            if reason is None:
                sl = float(c.get("stop_loss_pct", 0))
                tp = float(c.get("take_profit_pct", 0))
                mh = float(c.get("max_hold_hours", 0))
                if net is not None and sl > 0 and net / self.notional(pos) * 100 <= -sl:
                    reason = "stop_loss"
                elif net is not None and tp > 0 and net / self.notional(pos) * 100 >= tp:
                    reason = "take_profit"
                elif f and f["rate"] * 100 < float(c["exit_funding_pct"]):
                    reason = "funding_low"
                elif mh > 0 and now - pos["opened"] >= mh * 3600:
                    reason = "max_hold"
            if reason:
                pos["closing_reason"] = reason
                if self.close_legs(pos):
                    self.finalize(pos, reason)
