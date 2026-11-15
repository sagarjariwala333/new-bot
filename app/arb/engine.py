"""Shared logic for the three modules: positions, closing, journal, PnL."""
import logging

from app.arb.dec import D
from app.arb.fills import Leg
from app.arb.store import EngineState, iso, utc_now

DUST = 1e-12


class Engine:
    module = ""

    def __init__(self, market, mode, cfg, ctx, make_broker, label=""):
        self.market, self.mode, self.cfg, self.ctx = market, mode, cfg, ctx
        self.name = "%s/%s/%s" % (self.module, market, mode)
        self.log = ctx.log
        self.st = EngineState(ctx.s["data_folder"], self.name.replace("/", "_"))
        if self.st.wallets is None:
            self.st.wallets = {}
        self.broker = make_broker(self.st)
        self.board = []                       # best current plays (for the board)
        self.pos = self.st.positions
        self.exit_only = False                # True: module switched off but trades are open -> only exits

    # ---- helpers ----
    def fee(self, ex, market=None):
        return self.ctx.fee(ex, market or self.market)

    def base_blocked(self, base):
        return any(p["base"] == base for p in self.pos)

    def save(self):
        self.st.save()

    def new_leg_rec(self, ex, market, base, side, qty, px, fee):
        return {"ex": ex, "market": market, "base": base, "side": side, "qty0": qty, "qty": qty,
                "entry_px": px, "fee": fee, "exit_val": 0.0, "exit_q": 0.0}

    def nid(self):
        """Trade id: module + mode letter (L live, M mock, S live-mock 'shadow') + number."""
        code = {"live": "L", "mock": "M", "live-mock": "S"}.get(self.mode, "X")
        return self.st.next_id(self.module[:3] + code)

    def new_pos(self, kind, base, size, legs, **extra):
        p = {"id": self.nid(), "kind": kind, "market": self.market,
             "base": base, "opened": self.ctx.now(), "opened_utc": iso(), "size_usdt": size,
             "legs": legs, "adj": 0.0, "funding": 0.0, "close_fails": 0}
        p.update(extra)
        self.pos.append(p)
        self.save()
        if kind != "held":
            try:
                self.ctx.trade_event("open", self.mode, "opened %s/%s %s, size %.2f USDT (%s)"
                                     % (self.module, p["market"], base, size, p["id"]))
            except Exception:
                pass
        return p

    # ---- numbers ----
    @staticmethod
    def leg_dir(l):
        return 1.0 if l["side"] == "buy" else -1.0

    def unreal(self, pos, exit_fee=True):
        """Net PnL (USDT) if every remaining leg were closed at the touch right now. None if no price."""
        total = pos["adj"] + pos["funding"]
        for l in pos["legs"]:
            total -= l["fee"]
            total += self.leg_dir(l) * (l["exit_val"] - l["entry_px"] * l["exit_q"])
            if l["qty"] > DUST:
                b = self.ctx.book(l["ex"], l["market"], l["base"])
                if not b:
                    return None
                px = b["bid"] if l["side"] == "buy" else b["ask"]
                total += self.leg_dir(l) * (px - l["entry_px"]) * l["qty"]
                if exit_fee:
                    total -= l["qty"] * px * self.fee(l["ex"], l["market"])
        return total

    def final_pnl(self, pos):
        pnl = pos["adj"] + pos["funding"]
        fees = 0.0
        for l in pos["legs"]:
            pnl += self.leg_dir(l) * (l["exit_val"] - l["entry_px"] * l["exit_q"]) - l["fee"]
            fees += l["fee"]
        return pnl, fees

    def notional(self, pos):
        return max(pos["size_usdt"], 1e-9)

    # ---- closing ----
    def _is_dust(self, l):
        ins = self.ctx.ins(l["ex"], l["market"], l["base"])
        floor = float(max(ins.min_qty, ins.qty_step)) if ins else 1e-9
        return l["qty"] < max(floor, DUST)

    def close_legs(self, pos, only=None):
        """Send closing orders for every open leg. Returns True when everything is closed."""
        todo, legs = [], []
        for i, l in enumerate(pos["legs"]):
            if only is not None and i not in only:
                continue
            if self._is_dust(l):
                continue
            b = self.ctx.book(l["ex"], l["market"], l["base"])
            if not b:
                continue
            ins = self.ctx.ins(l["ex"], l["market"], l["base"])
            side = "sell" if l["side"] == "buy" else "buy"
            px = b["bid"] if side == "sell" else b["ask"]
            qty = l["qty"]
            if l["market"] == "spot" and side == "sell":
                have = self.broker.sellable(l["ex"], l["market"], l["base"])
                if have is not None:
                    qty = min(qty, have)
            if qty <= 0:
                continue
            todo.append(i)
            legs.append(Leg(l["ex"], l["market"], l["base"], side, qty, px, ins,
                            top=b["bq"] if side == "sell" else b["aq"], closing=True))
        if legs:
            fills = self.broker.exec_legs(legs)
            for i, leg, f in zip(todo, legs, fills):
                if f.status == "error":
                    self.log.warning("[%s] close problem: %s", self.name, f.error)
                if f.got:
                    l = pos["legs"][i]
                    q = min(f.qty, l["qty"])
                    l["qty"] -= q
                    l["exit_q"] += q
                    l["exit_val"] += f.px * q
                    l["fee"] += f.fee * (q / f.qty)
        done = all(self._is_dust(l) for l in pos["legs"])
        if not done:
            pos["close_fails"] += 1
            if pos["close_fails"] in (3, 20):
                self.alert("%s %s could NOT be closed after %d tries - check the exchange" % (pos["id"], pos["base"], pos["close_fails"]))
            self.ctx.note("closefail-" + pos["id"], logging.WARNING,
                          "[%s] %s %s NOT fully closed yet (try %d). Remaining: %s",
                          self.name, pos["id"], pos["base"], pos["close_fails"],
                          ", ".join("%s %s %.8g" % (l["ex"], l["side"], l["qty"]) for l in pos["legs"]
                                    if not self._is_dust(l)))
        self.save()
        return done

    def finalize(self, pos, reason, detail=""):
        pnl, fees = self.final_pnl(pos)
        size = self.notional(pos)
        row = {"id": pos["id"], "module": self.module, "market": pos["market"], "mode": self.mode,
               "base": pos["base"], "opened_utc": pos["opened_utc"], "closed_utc": iso(),
               "detail": detail or self.describe(pos),
               "qty": max(l["qty0"] for l in pos["legs"]), "size_usdt": round(size, 4),
               "fees_usdt": round(fees, 6), "funding_usdt": round(pos["funding"], 6),
               "net_pnl_usdt": round(pnl, 6), "net_pnl_pct": round(pnl / size * 100, 5),
               "reason": reason}
        self.ctx.journal.add(row)
        self.pos.remove(pos)
        t = self.st.totals
        t["trades"] += 1
        t["net_pnl_usdt"] += pnl
        t["wins"] += 1 if pnl > 0 else 0
        self.ctx.risk.record(pnl, self.mode)
        self.save()
        self.log.info("[%s] CLOSED %s %s %s: net %+.4f USDT (%+.4f%%), fees %.4f",
                      self.name, pos["id"], pos["base"], reason, pnl, row["net_pnl_pct"], fees)

    @staticmethod
    def describe(pos):
        return "; ".join("%s %s %s %.8g@%.8g->%.8g" % (
            l["side"], l["ex"], l["market"], l["qty0"], l["entry_px"],
            (l["exit_val"] / l["exit_q"]) if l["exit_q"] else 0) for l in pos["legs"])

    # ---- held single legs (one-leg rule) ----
    def hold_cfg(self, market):
        return self.ctx.s["one_leg"][market]

    def alert(self, text):
        if self.mode == "live":                  # only real money raises a phone alert
            try:
                self.ctx.alert("ARBITRAGE " + text)
            except Exception:
                pass

    def make_held(self, base, size, leg_rec, why):
        pos = self.new_pos("held", base, size, [leg_rec], why=why)
        self.alert("HOLDING %s %s %.8g on %s (%s)" % (leg_rec["side"], base, leg_rec["qty"], leg_rec["ex"], why))
        self.log.warning("[%s] HOLDING %s %s %.8g on %s (%s). New trades on %s %s.", self.name,
                         leg_rec["side"], base, leg_rec["qty"], leg_rec["ex"], why, base,
                         "BLOCKED" if self.hold_cfg(leg_rec["market"]).get("block_pair_while_held", True)
                         else "still allowed")
        return pos

    def manage_held(self, pos, now):
        l = pos["legs"][0]
        hc = self.hold_cfg(l["market"])
        b = self.ctx.book(l["ex"], l["market"], l["base"])
        if not b:
            return
        net = self.unreal(pos)
        if net is None:
            return
        pct = net / max(l["entry_px"] * l["qty0"], 1e-9) * 100
        reason = None
        target = float(hc.get("hold_profit_pct", 0))
        stop = float(hc.get("hold_stop_loss_pct", 0))
        mins = float(hc.get("hold_max_minutes", 0))
        if pct >= target:
            reason = "held_profit_hit"
        elif stop > 0 and pct <= -stop:
            reason = "held_stop_loss"
        elif mins > 0 and (now - pos["opened"]) / 60.0 >= mins:
            reason = "held_timeout"
        if reason and self.close_legs(pos):
            self.finalize(pos, reason)

    def trim(self, leg, side, fill, extra):
        """One leg filled more than the other: close the surplus so both sides match.
        Returns (pnl in USDT of that surplus, quantity really closed)."""
        opp = "sell" if side == "buy" else "buy"
        b = self.ctx.book(leg.ex, leg.market, leg.base) or {}
        px = b.get("bid" if opp == "sell" else "ask", leg.ref_px)
        t = self.broker.exec_legs([Leg(leg.ex, leg.market, leg.base, opp, extra, px, leg.ins,
                                       closing=True)])[0]
        d = 1.0 if side == "buy" else -1.0
        if not t.got:
            self.log.warning("[%s] could not trim surplus %.8g %s on %s (it stays in the position)",
                             self.name, extra, leg.base, leg.ex)
            return 0.0, 0.0
        return d * (t.px - fill.px) * t.qty - fill.fee * t.qty / fill.qty - t.fee, t.qty

    def one_leg(self, base, size, side, fill, leg, why, action=None):
        """One side filled, the other did not. Apply the user's rule."""
        hc = self.hold_cfg(leg.market) if action is None else {}
        rec = self.new_leg_rec(leg.ex, leg.market, base, side, fill.net_qty if side == "buy" else fill.qty,
                               fill.px, fill.fee)
        action = action or hc.get("action", "close_filled")
        if action == "close_filled":
            pos = self.new_pos("held", base, size, [rec], why=why)
            self.log.warning("[%s] ONE LEG only (%s): closing the filled %s on %s at once",
                             self.name, why, side, leg.ex)
            self.alert("ONE LEG only on %s %s (%s): closing the filled %s on %s at once" % (self.name, base, why, side, leg.ex))
            if self.close_legs(pos):
                self.finalize(pos, "one_leg_closed")
            else:
                pos["why"] = why + " (close failed, will retry)"
        else:
            self.make_held(base, size, rec, why)

    def fills_problem(self, fills):
        for f in fills:
            if f.status == "error" or (f.error and not f.got):
                self.log.warning("[%s] order problem: %s", self.name, f.error)

    # ---- common loop pieces ----
    def entries_allowed(self):
        if self.exit_only:
            return False
        ok, _ = self.ctx.risk.entry_allowed(self.mode)
        return ok

    def status(self):
        t = self.st.totals
        held = sum(1 for p in self.pos if p["kind"] == "held")
        return "%s: open=%d held=%d trades=%d net=%+.4f USDT" % (
            self.name, len(self.pos) - held, held, t["trades"], t["net_pnl_usdt"])
