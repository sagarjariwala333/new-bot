"""MOCK broker: fills at the best bid/ask (+ slippage), whole size, instantly.
Keeps fake wallets in the engine's state file. Never talks to an exchange."""
from app.arb.fills import Fill

DUST = 1e-12


class MockBroker:
    mode = "mock"

    def __init__(self, wallets, fee_fn, slippage_pct, leverage, start_usdt, inventory_usdt):
        self.w = wallets
        self.fee_fn = fee_fn               # (ex, market) -> taker fraction
        self.slip = slippage_pct / 100.0
        self.lev = float(leverage)
        self.start_usdt = start_usdt       # {exchange: usdt per wallet}
        self.inventory = float(inventory_usdt)   # spot: pretend you hold this much of every coin
        self.w.setdefault("bal", {})
        self.w.setdefault("fpos", {})

    # ---- wallets ----
    def _bal(self, ex, market):
        key = "%s|%s" % (ex, market)
        if key not in self.w["bal"]:
            self.w["bal"][key] = {"USDT": float(self.start_usdt.get(ex, 0.0))}
        return self.w["bal"][key]

    def _coin(self, ex, market, coin, px):
        b = self._bal(ex, market)
        if coin not in b:
            b[coin] = self.inventory / px if (self.inventory > 0 and px > 0) else 0.0
        return b

    def _locked(self, ex):
        return sum(p["margin"] for k, p in self.w["fpos"].items() if k.startswith(ex + "|"))

    def free_margin(self, ex):
        return self._bal(ex, "futures")["USDT"] - self._locked(ex)

    def credit(self, ex, market, amount):
        self._bal(ex, market)["USDT"] += amount

    def total_usdt(self):
        return sum(b.get("USDT", 0.0) for b in self.w["bal"].values())

    def sellable(self, ex, market, base):
        if market != "spot":
            return None
        return self._bal(ex, "spot").get(base, 0.0)

    # ---- checks ----
    def _px(self, leg):
        return leg.ref_px * (1 + self.slip if leg.side == "buy" else 1 - self.slip)

    def can_do(self, leg):
        px, fee = self._px(leg), self.fee_fn(leg.ex, leg.market)
        if leg.market == "spot":
            b = self._coin(leg.ex, "spot", leg.base, leg.ref_px)
            qa = leg.ins.quote if leg.ins else "USDT"
            if leg.side == "buy":
                if b.get(qa, 0.0) + DUST < leg.qty * px * (1 + fee):
                    return False, "not enough %s on %s spot" % (qa, leg.ex)
            elif b[leg.base] + DUST < leg.qty:
                return False, "not enough %s on %s spot" % (leg.base, leg.ex)
            return True, ""
        key = "%s|%s" % (leg.ex, leg.base)
        if leg.closing:
            p = self.w["fpos"].get(key)
            return (True, "") if p and p["qty"] + DUST >= leg.qty else (False, "no position to close")
        if key in self.w["fpos"]:
            return False, "already a position in %s on %s" % (leg.base, leg.ex)
        if self.free_margin(leg.ex) + DUST < leg.qty * px / self.lev + leg.qty * px * fee:
            return False, "not enough margin on %s futures" % leg.ex
        return True, ""

    def can_do_all(self, legs):
        for l in legs:
            ok, why = self.can_do(l)
            if not ok:
                return False, why
        return True, ""

    # ---- fills ----
    def _fill(self, leg):
        ok, why = self.can_do(leg)
        if not ok:
            return Fill("none", error=why)
        px, q = self._px(leg), leg.qty
        fee = q * px * self.fee_fn(leg.ex, leg.market)
        if leg.market == "spot":
            b = self._coin(leg.ex, "spot", leg.base, leg.ref_px)
            qa = leg.ins.quote if leg.ins else "USDT"
            if leg.side == "buy":
                b[qa] = b.get(qa, 0.0) - (q * px + fee)
                b[leg.base] += q
            else:
                b[leg.base] = max(0.0, b[leg.base] - q)
                b[qa] = b.get(qa, 0.0) + q * px - fee
        else:
            b = self._bal(leg.ex, "futures")
            key = "%s|%s" % (leg.ex, leg.base)
            if leg.closing:
                p = self.w["fpos"][key]
                d = 1.0 if p["side"] == "buy" else -1.0
                part = q / p["qty"]
                released = p["margin"] * part
                b["USDT"] += (px - p["entry"]) * q * d - fee
                p["qty"] -= q
                p["margin"] -= released
                if p["qty"] <= DUST:
                    del self.w["fpos"][key]
            else:
                b["USDT"] -= fee
                self.w["fpos"][key] = {"side": leg.side, "qty": q, "entry": px,
                                       "margin": q * px / self.lev}
        return Fill("filled", q, px, fee, order_id="mock")

    def exec_legs(self, legs):
        return [self._fill(l) for l in legs]
