"""Result of an order."""


class Fill:
    """status: filled | partial | none | error.   qty is in BASE coins.
    qty = bought/sold amount; net_qty = what you really hold after fees
    taken in the coin itself (spot buys)."""

    def __init__(self, status="none", qty=0.0, px=0.0, fee=0.0, order_id="", error="",
                 net_qty=None):
        self.status = status
        self.qty = float(qty)
        self.px = float(px)
        self.fee = float(fee)       # quote currency, estimated from the fee rate
        self.order_id = order_id
        self.error = error
        self.net_qty = float(qty if net_qty is None else net_qty)

    @property
    def got(self):
        return self.qty > 0

    def __repr__(self):
        return "Fill(%s qty=%s px=%s fee=%s %s)" % (self.status, self.qty, self.px,
                                                    self.fee, self.error)


class Leg:
    """One order to place. qty in BASE coins. closing=True for exits (reduce-only on futures)."""

    def __init__(self, ex, market, base, side, qty, ref_px, ins, top=0.0, closing=False):
        self.ex, self.market, self.base, self.side = ex, market, base, side
        self.qty = float(qty)
        self.ref_px = float(ref_px)       # best price in the book when the decision was made
        self.ins = ins                    # Instr (trading rules) - may be None in the mock
        self.top = float(top)             # size available at that best price (base coins)
        self.closing = closing

    def __repr__(self):
        return "Leg(%s %s %s %s qty=%s @%s%s)" % (self.ex, self.market, self.side, self.base,
                                                  self.qty, self.ref_px, " close" if self.closing else "")
