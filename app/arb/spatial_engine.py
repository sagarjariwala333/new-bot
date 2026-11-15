"""Spatial arbitrage: buy where it is cheaper, sell where it is dearer (Binance vs OKX).

futures: long on the cheaper exchange + short on the dearer one, close both when the gap closes.
spot:    buy on one exchange and sell on the other at once (needs USDT on one side, coins on the other).
"""
import logging

from app.arb.dec import D, floor_to, lcm_step
from app.arb.engine import Engine
from app.arb.fills import Leg


class Spatial(Engine):
    module = "spatial"

    def __init__(self, market, mode, cfg, ctx, make_broker):
        super().__init__(market, mode, cfg, ctx, make_broker)
        self.A, self.B = ctx.exchanges[0], ctx.exchanges[1]
        self.rounds = 2 if market == "futures" else 1       # fee rounds (open+close vs one swap)
        self.nfills = 4 if market == "futures" else 2       # price-slippage hits

    # ---- gaps ----
    def compute_edges(self):
        m, slip = self.market, float(self.cfg.get("slippage_pct", 0))
        out = []
        for base in self.ctx.universe.get(m, []):
            bA, bB = self.ctx.book(self.A, m, base), self.ctx.book(self.B, m, base)
            if not (bA and bB):
                continue
            for buy, sell, bb, sb in ((self.A, self.B, bA, bB), (self.B, self.A, bB, bA)):
                gap = (sb["bid"] - bb["ask"]) / bb["ask"] * 100
                fees = self.rounds * (self.fee(buy) + self.fee(sell)) * 100
                out.append({"base": base, "buy": buy, "sell": sell, "ask": bb["ask"], "bid": sb["bid"],
                            "aq": bb["aq"], "bq": sb["bq"], "gap": gap,
                            "net": gap - fees - self.nfills * slip})
        out.sort(key=lambda e: e["net"], reverse=True)
        return out

    # ---- main step ----
    def step(self, now):
        self.manage(now)
        edges = self.compute_edges()
        self.board = edges[:10]
        self.enter(edges, now)

    # ---- entries ----
    def enter(self, edges, now):
        if not self.entries_allowed():
            return
        minp = float(self.cfg["min_profit_pct"])
        maxo = int(self.cfg.get("max_open_trades", 1))
        per_loop = int(self.cfg.get("max_entries_per_loop", 3))
        done = set()
        for e in edges:
            if e["net"] < minp or len(done) >= per_loop:
                break
            if e["base"] in done or self._blocked(e["base"]):
                continue
            if sum(1 for p in self.pos if p["kind"] == "open") >= maxo:
                break
            done.add(e["base"])
            self.try_enter(e, now)

    def _blocked(self, base):
        """A pair with an open/held position takes no new trade (spot can be set to allow it)."""
        ps = [p for p in self.pos if p["base"] == base]
        if not ps:
            return False
        if self.market == "spot" and not self.hold_cfg("spot").get("block_pair_while_held", True) \
                and all(p["kind"] == "held" for p in ps):
            return False
        return True

    def try_enter(self, e, now):
        m, base = self.market, e["base"]
        ib, is_ = self.ctx.ins(e["buy"], m, base), self.ctx.ins(e["sell"], m, base)
        if not ib or not is_:
            return
        mts = float(self.ctx.s["pairs"].get("min_top_size_usdt", 0))
        if min(e["aq"], e["bq"]) * e["ask"] < mts:
            return        # too thin at the best price (settings: pairs.min_top_size_usdt)
        size = self.ctx.risk.cap(float(self.cfg["trade_size_usdt"]), self.mode)
        qty = D(min(size / e["ask"], e["aq"], e["bq"]))
        qty = floor_to(qty, lcm_step(ib.qty_step, is_.qty_step))
        if not (ib.size_ok(qty, e["ask"]) and is_.size_ok(qty, e["bid"])):
            self.ctx.note("small-%s-%s" % (m, base), logging.INFO,
                          "[%s] %s: gap %.3f%% but size too small/thin at best price (top size %.6g)",
                          self.name, base, e["net"], min(e["aq"], e["bq"]))
            return
        legs = [Leg(e["buy"], m, base, "buy", float(qty), e["ask"], ib, top=e["aq"]),
                Leg(e["sell"], m, base, "sell", float(qty), e["bid"], is_, top=e["bq"])]
        ok, why = self.broker.can_do_all(legs)
        if not ok:
            self.ctx.note("cant-%s-%s" % (m, base), logging.INFO, "[%s] %s: net %.3f%% but skipped: %s",
                          self.name, base, e["net"], why)
            return
        self.log.info("[%s] ENTER %s: buy %s @%.8g, sell %s @%.8g, qty %s, expected net %.3f%%",
                      self.name, base, e["buy"], e["ask"], e["sell"], e["bid"], qty, e["net"])
        fills = self.broker.exec_legs(legs)
        self.fills_problem(fills)
        self.after_entry(e, legs, fills, float(qty) * e["ask"])

    def after_entry(self, e, legs, fills, size):
        m, base = self.market, e["base"]
        fb, fs = fills
        if not fb.got and not fs.got:
            self.log.info("[%s] %s: nothing filled, no position", self.name, base)
            return
        if fb.got != fs.got:
            f, leg, side = (fb, legs[0], "buy") if fb.got else (fs, legs[1], "sell")
            self.one_leg(base, size, side, f, leg, "other leg did not fill")
            return
        if m == "futures":
            q = min(fb.qty, fs.qty)
            adj, kept = 0.0, {}
            for f, leg, side in ((fb, legs[0], "buy"), (fs, legs[1], "sell")):
                rq = leg.ins.round_qty(D(f.qty - q)) if (leg.ins and f.qty - q > 0) else D(0)
                trimmed = 0.0
                if rq > 0 and rq >= leg.ins.min_qty:
                    a, trimmed = self.trim(leg, side, f, float(rq))
                    adj += a
                kept[side] = f.qty - trimmed          # what is REALLY still open on that exchange
            recs = [self.new_leg_rec(legs[0].ex, m, base, "buy", kept["buy"], fb.px,
                                     fb.fee * kept["buy"] / fb.qty),
                    self.new_leg_rec(legs[1].ex, m, base, "sell", kept["sell"], fs.px,
                                     fs.fee * kept["sell"] / fs.qty)]
            p = self.new_pos("open", base, q * (fb.px + fs.px) / 2, recs)
            p["adj"] = adj
            self.save()
            return
        # spot: both sides done at once
        matched = min(fb.qty, fs.qty)            # gross amounts; coin-fee shortfall only affects inventory
        recs = [self.new_leg_rec(legs[0].ex, m, base, "buy", matched, fb.px, fb.fee * matched / fb.qty),
                self.new_leg_rec(legs[1].ex, m, base, "sell", matched, fs.px, fs.fee * matched / fs.qty)]
        pnl = matched * (fs.px - fb.px) - recs[0]["fee"] - recs[1]["fee"]
        self._journal_instant(base, size, recs, pnl, "spot_pair")
        rb, rs = fb.qty - matched, fs.qty - matched
        if rb > 0 and legs[0].ins and rb >= float(legs[0].ins.min_qty):
            self.make_held(base, size, self.new_leg_rec(legs[0].ex, m, base, "buy", rb * fb.net_qty / fb.qty,
                                                        fb.px, fb.fee * rb / fb.qty), "buy side filled more")
        if rs > 0 and legs[1].ins and rs >= float(legs[1].ins.min_qty):
            self.make_held(base, size, self.new_leg_rec(legs[1].ex, m, base, "sell", rs, fs.px,
                                                        fs.fee * rs / fs.qty), "sell side filled more")

    def _journal_instant(self, base, size, recs, pnl, reason):
        from app.arb.store import iso
        fees = sum(r["fee"] for r in recs)
        self.ctx.journal.add({
            "id": self.nid(), "module": self.module, "market": self.market, "mode": self.mode,
            "base": base, "opened_utc": iso(), "closed_utc": iso(),
            "detail": "; ".join("%s %s %.8g@%.8g" % (r["side"], r["ex"], r["qty0"], r["entry_px"])
                                for r in recs),
            "qty": recs[0]["qty0"], "size_usdt": round(size, 4), "fees_usdt": round(fees, 6),
            "funding_usdt": 0, "net_pnl_usdt": round(pnl, 6),
            "net_pnl_pct": round(pnl / max(size, 1e-9) * 100, 5), "reason": reason})
        t = self.st.totals
        t["trades"] += 1
        t["net_pnl_usdt"] += pnl
        t["wins"] += 1 if pnl > 0 else 0
        self.ctx.risk.record(pnl, self.mode)
        self.save()
        self.log.info("[%s] DONE %s: net %+.4f USDT (%.4f fees)", self.name, base, pnl, fees)

    # ---- exits ----
    def manage(self, now):
        c = self.cfg
        for pos in list(self.pos):
            if pos["kind"] == "held":
                self.manage_held(pos, now)
                continue
            net = self.unreal(pos)
            if net is None:
                continue
            pct = net / self.notional(pos) * 100
            reason = None
            sl = float(c.get("stop_loss_pct", 0))
            mh = float(c.get("max_hold_seconds", 0))
            if sl > 0 and pct <= -sl:
                reason = "stop_loss"
            elif pct >= float(c.get("exit_profit_pct", 0)):
                reason = "take_profit"
            elif mh > 0 and now - pos["opened"] >= mh:
                reason = "max_hold"
            if reason is None and pos.get("close_fails", 0) > 0:
                reason = pos.get("closing_reason")        # keep trying a close that half-failed
            if reason:
                pos["closing_reason"] = reason
                if self.close_legs(pos):
                    self.finalize(pos, reason)
