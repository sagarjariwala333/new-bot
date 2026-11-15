"""Triangular arbitrage inside ONE exchange (spot): USDT -> coin -> bridge coin -> USDT.

Locked by default (see locks.py). Three market orders in a row: if a later leg fails, whatever
coin is left over is sold back to USDT at once; if even that fails it is kept as a 'held' leftover.
"""
import logging

from app.arb.dec import D, floor_to
from app.arb.engine import Engine
from app.arb.fills import Leg


class Cycle:
    def __init__(self, ex, x, b, xu, xb, bu):
        self.ex, self.x, self.b = ex, x, b
        self.xu, self.xb, self.bu = xu, xb, bu       # Instr objects: X/USDT, X/B, B/USDT


class Triangular(Engine):
    module = "triangular"

    def __init__(self, mode, cfg, ctx, make_broker):
        super().__init__("spot", mode, cfg, ctx, make_broker)
        self.cycles, self._stamp = [], -1

    # ---- cycles ----
    def rebuild(self):
        self.cycles = []
        bridges = self.cfg.get("bridge_coins", ["BTC", "ETH"])
        for ex in self.ctx.exchanges:
            ins = self.ctx.instr.get((ex, "spot"), {})
            by = {(i.base, i.quote): i for i in ins.values()}
            for x in self.ctx.tri_bases.get(ex, []):
                if (x, "USDT") not in by:
                    continue
                for b in bridges:
                    if b != x and (b, "USDT") in by and (x, b) in by:
                        self.cycles.append(Cycle(ex, x, b, by[(x, "USDT")], by[(x, b)], by[(b, "USDT")]))
        self._stamp = self.ctx.stamp

    def compute_edges(self):
        slip = float(self.cfg.get("slippage_pct", 0))
        out = []
        for cy in self.cycles:
            bk = self.ctx.books.get((cy.ex, "spot"), {})
            xu, xb, bu = bk.get(cy.xu.symbol), bk.get(cy.xb.symbol), bk.get(cy.bu.symbol)
            if not (xu and xb and bu):
                continue
            f = (1 - self.fee(cy.ex)) ** 3
            fwd = bu["bid"] * xb["bid"] / xu["ask"]
            rev = xu["bid"] / (bu["ask"] * xb["ask"])
            for direction, ratio in (("fwd", fwd), ("rev", rev)):
                out.append({"cy": cy, "dir": direction, "ex": cy.ex, "base": cy.x, "bridge": cy.b,
                            "net": (ratio * f - 1) * 100 - 3 * slip})
        out.sort(key=lambda e: e["net"], reverse=True)
        return out

    def step(self, now):
        if self._stamp != self.ctx.stamp:
            self.rebuild()
        self.manage(now)
        edges = self.compute_edges()
        self.board = edges[:10]
        self.enter(edges, now)

    def enter(self, edges, now):
        if not self.entries_allowed():
            return
        done = 0
        for e in edges:
            if e["net"] < float(self.cfg["min_profit_pct"]) or done >= int(self.cfg.get("max_entries_per_loop", 1)):
                break
            cy = e["cy"]
            if self.base_blocked(cy.x) or self.base_blocked(cy.b):
                continue
            done += 1
            self.run_cycle(e, now)

    # ---- one cycle ----
    def _usdt_value(self, ex, asset):
        if asset == "USDT":
            return 1.0
        b = self.ctx.book(ex, "spot", asset)
        return (b["bid"] + b["ask"]) / 2 if b else 0.0

    def run_cycle(self, e, now):
        cy, ex = e["cy"], e["ex"]
        bk = self.ctx.books.get((ex, "spot"), {})
        steps = ([(cy.xu, "buy"), (cy.xb, "sell"), (cy.bu, "sell")] if e["dir"] == "fwd"
                 else [(cy.bu, "buy"), (cy.xb, "buy"), (cy.xu, "sell")])
        # size cap from the thinnest leg (in USDT)
        size = self.ctx.risk.cap(float(self.cfg["trade_size_usdt"]), self.mode)
        for ins, side in steps:
            b = bk.get(ins.symbol)
            if not b:
                return
            px = b["ask"] if side == "buy" else b["bid"]
            top = (b["aq"] if side == "buy" else b["bq"]) * px * self._usdt_value(ex, ins.quote)
            size = min(size, top)
        self.log.info("[%s] ENTER %s %s via %s on %s: expected net %.3f%%, size %.2f", self.name,
                      cy.x, e["dir"], cy.b, ex, e["net"], size)
        hold = {"USDT": 0.0}       # what is really in the wallet (fees already taken out)
        gross = {"USDT": 0.0}      # used for the profit figure (fees counted separately)
        fees, reason = 0.0, "cycle_done"
        for i, (ins, side) in enumerate(steps):
            b = bk.get(ins.symbol)
            px = b["ask"] if side == "buy" else b["bid"]
            if i == 0:
                qty = size / px
            elif side == "sell":
                qty = hold.get(ins.base, 0.0)
            else:
                qty = hold.get(ins.quote, 0.0) * (1 - 2 * self.fee(ex)) / px
            qty_d = ins.round_qty(D(qty))
            if not ins.size_ok(qty_d, px):
                reason = "stopped_leg%d_too_small" % (i + 1)
                break
            leg = Leg(ex, "spot", ins.base, side, float(qty_d), px, ins,
                      top=b["aq"] if side == "buy" else b["bq"])
            ok, why = self.broker.can_do_all([leg])
            if not ok:
                reason = "stopped_leg%d_%s" % (i + 1, why[:40])
                break
            f = self.broker.exec_legs([leg])[0]
            self.fills_problem([f])
            if not f.got:
                reason = "stopped_leg%d_not_filled" % (i + 1)
                break
            self._apply(hold, gross, ins, side, f)
            fees += f.fee * self._usdt_value(ex, ins.quote)
            if f.status == "partial":
                reason = "partial_leg%d" % (i + 1)
        # sell whatever coin is left over back into USDT
        left = []
        for asset, q in list(hold.items()):
            if asset == "USDT" or q <= 0:
                continue
            ins = {cy.x: cy.xu, cy.b: cy.bu}.get(asset)
            if ins is None:
                continue
            for attempt in range(2):
                b = bk.get(ins.symbol)
                qd = ins.round_qty(D(min(q, self.broker.sellable(ex, "spot", asset) or q)))
                if not b or not ins.size_ok(qd, b["bid"]):
                    break
                f = self.broker.exec_legs([Leg(ex, "spot", asset, "sell", float(qd), b["bid"], ins,
                                               top=b["bq"], closing=True)])[0]
                if f.got:
                    self._apply(hold, gross, ins, "sell", f)
                    fees += f.fee
                    q = hold[asset]
                    if reason == "cycle_done":
                        reason = "cycle_done_swept"
                    if q <= 0 or not ins.size_ok(ins.round_qty(D(q)), b["bid"]):
                        break
            if hold[asset] > 0 and ins.size_ok(ins.round_qty(D(hold[asset])), px_or(bk, ins, "bid")):
                left.append((asset, hold[asset], ins))
        pnl = gross["USDT"] - fees
        self._journal(cy, e, size, fees, pnl, reason)
        for asset, q, ins in left:
            b = self.ctx.book(ex, "spot", asset)
            mid = (b["bid"] + b["ask"]) / 2 if b else 0.0
            rec = self.new_leg_rec(ex, "spot", asset, "buy", q, mid, 0.0)
            self.new_pos("held", asset, q * mid, [rec], why="triangular leftover")
            self.log.warning("[%s] LEFTOVER %.8g %s on %s could not be sold yet; will keep trying",
                             self.name, q, asset, ex)

    @staticmethod
    def _apply(hold, gross, ins, side, f):
        if side == "buy":
            hold[ins.base] = hold.get(ins.base, 0.0) + f.net_qty
            hold[ins.quote] = hold.get(ins.quote, 0.0) - f.qty * f.px
            gross[ins.base] = gross.get(ins.base, 0.0) + f.qty
            gross[ins.quote] = gross.get(ins.quote, 0.0) - f.qty * f.px
        else:
            hold[ins.base] = hold.get(ins.base, 0.0) - f.qty
            hold[ins.quote] = hold.get(ins.quote, 0.0) + f.qty * f.px - f.fee
            gross[ins.base] = gross.get(ins.base, 0.0) - f.qty
            gross[ins.quote] = gross.get(ins.quote, 0.0) + f.qty * f.px

    def _journal(self, cy, e, size, fees, pnl, reason):
        from app.arb.store import iso
        self.ctx.journal.add({
            "id": self.nid(), "module": self.module, "market": "spot", "mode": self.mode,
            "base": "%s/%s" % (cy.x, cy.b), "opened_utc": iso(), "closed_utc": iso(),
            "detail": "%s %s via %s on %s" % (cy.x, e["dir"], cy.b, cy.ex), "qty": 0,
            "size_usdt": round(size, 4), "fees_usdt": round(fees, 6), "funding_usdt": 0,
            "net_pnl_usdt": round(pnl, 6), "net_pnl_pct": round(pnl / max(size, 1e-9) * 100, 5),
            "reason": reason})
        t = self.st.totals
        t["trades"] += 1
        t["net_pnl_usdt"] += pnl
        t["wins"] += 1 if pnl > 0 else 0
        self.ctx.risk.record(pnl, self.mode)
        self.save()
        self.log.info("[%s] DONE %s/%s %s: net %+.4f USDT (%s)", self.name, cy.x, cy.b, e["dir"], pnl, reason)

    # leftovers are sold at market as soon as possible (no waiting for profit)
    def manage_held(self, pos, now):
        if self.close_legs(pos):
            self.finalize(pos, "leftover_sold")

    def manage(self, now):
        for pos in list(self.pos):
            self.manage_held(pos, now)


def px_or(bk, ins, key):
    b = bk.get(ins.symbol)
    return b[key] if b else 1e-18
