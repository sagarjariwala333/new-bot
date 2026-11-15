"""Binance / OKX signed clients for the arbitrage tab (LIVE; Step 3). Sync, run in the worker thread.

Futures are ISOLATED margin only (owner decision: the arbitrage must never touch the hub's cross margin).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from app import api_guard
from app.arb.dec import fmt
from app.arb.live_http import ApiError, Transport, definitely_rejected, guard_for

BINANCE_URLS = {
    ("futures", False): "https://fapi.binance.com",
    ("futures", True): "https://testnet.binancefuture.com",
    ("spot", False): "https://api.binance.com",
    ("spot", True): "https://testnet.binance.vision",
}
OKX_URL = "https://www.okx.com"


def _num(v, name, allow_empty=False):
    if allow_empty and v in ("", None):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ApiError("Bad number for %s: %r" % (name, v))


class BinanceClient:
    name = "binance"

    def __init__(self, market, testnet, key, secret, transport: Transport, timeout=10):
        self.market, self.testnet, self.key, self.secret = market, bool(testnet), key, secret
        self.tp, self.timeout = transport, timeout
        self.url = BINANCE_URLS[(market, self.testnet)]
        self.prefix = "/fapi/v1" if market == "futures" else "/api/v3"

    @property
    def tag(self):
        return "binance-%s[%s]" % (self.market, "testnet" if self.testnet else "live")

    @property
    def api_key(self):
        return self.key

    def _query(self, params):
        p = dict(params)
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 5000
        q = urlencode(p)
        return "%s&signature=%s" % (q, hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest())

    def _call(self, method, path, params, urgent=None):
        url = "%s%s?%s" % (self.url, path, self._query(params))
        weight = api_guard.binance_weight(method, path, params)
        if self.market == "spot" and path.endswith("/account"):
            weight = 20
        if urgent is None:
            urgent = api_guard.binance_urgent(method, path)
        return self.tp.request(method, url, guard=guard_for("binance", url), weight=weight, urgent=urgent,
                               headers={"X-MBX-APIKEY": self.key}, timeout=self.timeout)

    # ---- account ----
    def get_fees(self, ref_symbol="BTCUSDT"):
        if self.market == "futures":
            d = self._call("GET", "/fapi/v1/commissionRate", {"symbol": ref_symbol})
            if not isinstance(d, dict):
                raise ApiError("Unexpected fee reply")
            return {"maker": _num(d.get("makerCommissionRate"), "maker"), "taker": _num(d.get("takerCommissionRate"), "taker")}
        d = self._call("GET", "/api/v3/account/commission", {"symbol": ref_symbol})
        std = d.get("standardCommission") if isinstance(d, dict) else None
        if not isinstance(std, dict):
            raise ApiError("Unexpected spot fee reply")
        return {"maker": _num(std.get("maker"), "maker"), "taker": _num(std.get("taker"), "taker")}

    def get_balances(self, assets=None):
        out = {a: {"total": 0.0, "available": 0.0} for a in (assets or [])}
        if self.market == "futures":
            try:
                d = self._call("GET", "/fapi/v3/balance", {})
            except ApiError:
                d = self._call("GET", "/fapi/v2/balance", {})
            if not isinstance(d, list):
                raise ApiError("Unexpected balance reply")
            for row in d:
                a = row.get("asset")
                if assets is None or a in out:
                    out[a] = {"total": _num(row.get("balance"), "balance"),
                              "available": _num(row.get("availableBalance"), "availableBalance")}
            return out
        d = self._call("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        rows = d.get("balances") if isinstance(d, dict) else None
        if not isinstance(rows, list):
            raise ApiError("Unexpected spot balance reply")
        for row in rows:
            a = row.get("asset")
            if assets is None or a in out:
                free, locked = _num(row.get("free"), "free"), _num(row.get("locked"), "locked")
                out[a] = {"total": free + locked, "available": free}
        return out

    def one_way_mode(self):
        if self.market != "futures":
            return True
        d = self._call("GET", "/fapi/v1/positionSide/dual", {})
        return isinstance(d, dict) and str(d.get("dualSidePosition")).lower() == "false"

    def open_positions(self):
        """{symbol: signed base quantity} for every non-zero futures position on the account."""
        if self.market != "futures":
            return {}
        d = self._call("GET", "/fapi/v2/positionRisk", {})
        if not isinstance(d, list):
            raise ApiError("Unexpected positionRisk reply")
        out = {}
        for r in d:
            try:
                q = float(r.get("positionAmt") or 0)
            except (TypeError, ValueError):
                continue
            if q != 0:
                out[r["symbol"]] = q
        return out

    def set_isolated(self, ins):
        if self.market != "futures":
            return
        try:
            self._call("POST", "/fapi/v1/marginType", {"symbol": ins.symbol, "marginType": "ISOLATED"})
        except ApiError as e:
            if '"code":-4046' in str(e).replace(" ", ""):      # "No need to change margin type": already isolated
                return
            raise

    def set_leverage(self, ins, lev):
        if self.market == "futures":
            self._call("POST", "/fapi/v1/leverage", {"symbol": ins.symbol, "leverage": int(lev)})

    # ---- orders ----
    def _norm(self, d, ins, side):
        if not isinstance(d, dict) or "orderId" not in d:
            raise ApiError("Unexpected order reply")
        filled = float(d.get("executedQty") or 0)
        if self.market == "futures":
            avg = float(d.get("avgPrice") or 0)
            if avg == 0 and filled > 0:
                avg = float(d.get("cumQuote") or 0) / filled
        else:
            avg = float(d.get("cummulativeQuoteQty") or 0) / filled if filled else 0.0
        net = None
        fills = d.get("fills")
        if self.market == "spot" and side == "buy" and isinstance(fills, list):
            net = filled - sum(float(f.get("commission") or 0) for f in fills if f.get("commissionAsset") == ins.base)
        return {"id": str(d["orderId"]), "filled": filled, "avg": avg, "net": net,
                "open": d.get("status") in ("NEW", "PARTIALLY_FILLED", "PENDING_NEW"), "state": d.get("status", "")}

    def place(self, ins, side, qty, otype, price=None, tif="GTC", reduce_only=False, cl_id=None):
        p = {"symbol": ins.symbol, "side": side.upper(), "type": "MARKET" if otype == "market" else "LIMIT",
             "quantity": fmt(qty), "newOrderRespType": "RESULT" if self.market == "futures" else "FULL"}
        if otype != "market":
            p["price"] = fmt(price)
            p["timeInForce"] = tif
        if reduce_only and self.market == "futures":
            p["reduceOnly"] = "true"
        if cl_id:
            p["newClientOrderId"] = cl_id
        return self._norm(self._call("POST", self.prefix + "/order", p, urgent=True), ins, side)

    def get_order(self, ins, side, order_id=None, cl_id=None):
        p = {"symbol": ins.symbol}
        if order_id:
            p["orderId"] = order_id
        else:
            p["origClientOrderId"] = cl_id
        return self._norm(self._call("GET", self.prefix + "/order", p, urgent=True), ins, side)

    def cancel(self, ins, order_id):
        try:
            self._call("DELETE", self.prefix + "/order", {"symbol": ins.symbol, "orderId": order_id}, urgent=True)
        except ApiError as e:
            if not definitely_rejected(e):
                raise


class OKXClient:
    name = "okx"

    def __init__(self, market, demo, key, secret, passphrase, transport: Transport, timeout=10):
        self.market, self.testnet = market, bool(demo)
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.tp, self.timeout, self.url = transport, timeout, OKX_URL

    @property
    def tag(self):
        return "okx-%s[%s]" % (self.market, "demo" if self.testnet else "live")

    @property
    def api_key(self):
        return self.key

    def _headers(self, method, request_path, body):
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        sig = base64.b64encode(hmac.new(self.secret.encode(), (ts + method + request_path + body).encode(),
                                        hashlib.sha256).digest()).decode()
        h = {"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sig, "OK-ACCESS-TIMESTAMP": ts,
             "OK-ACCESS-PASSPHRASE": self.passphrase}
        if self.testnet:
            h["x-simulated-trading"] = "1"
        return h

    @staticmethod
    def _check(resp):
        if not isinstance(resp, dict) or resp.get("code") != "0":
            code = resp.get("code") if isinstance(resp, dict) else "?"
            msg = resp.get("msg") if isinstance(resp, dict) else "bad reply"
            raise ApiError("OKX error code %s: %s" % (code, msg))
        data = resp.get("data")
        if not isinstance(data, list):
            raise ApiError("OKX reply has no data list")
        return data

    def _get(self, path, params, urgent=False):
        q = urlencode(params)
        rp = path + ("?" + q if q else "")
        resp = self.tp.request("GET", self.url + rp, guard=guard_for("okx", self.url, self.key), weight=1, urgent=urgent,
                               headers=self._headers("GET", rp, ""), timeout=self.timeout, okx=True)
        return self._check(resp)

    def _post(self, path, body):
        raw = json.dumps(body, separators=(",", ":"))
        h = self._headers("POST", path, raw)
        h["Content-Type"] = "application/json"
        resp = self.tp.request("POST", self.url + path, guard=guard_for("okx", self.url, self.key), weight=1, urgent=True,
                               headers=h, data=raw.encode(), timeout=self.timeout, okx=True)
        return self._check(resp)

    # ---- account ----
    def get_fees(self, ref_symbol="BTC-USDT"):
        params = ({"instType": "SWAP", "instFamily": ref_symbol} if self.market == "futures"
                  else {"instType": "SPOT", "instId": ref_symbol})
        data = self._get("/api/v5/account/trade-fee", params)
        if not data:
            raise ApiError("OKX returned no fee data")
        d = data[0]
        maker, taker = ((d.get("makerU") or d.get("maker"), d.get("takerU") or d.get("taker"))
                        if self.market == "futures" else (d.get("maker"), d.get("taker")))
        return {"maker": -_num(maker, "maker"), "taker": -_num(taker, "taker")}

    def get_balances(self, assets=None):
        out = {a: {"total": 0.0, "available": 0.0} for a in (assets or [])}
        data = self._get("/api/v5/account/balance", {"ccy": ",".join(assets)} if assets else {})
        if not data:
            return out
        for row in data[0].get("details", []):
            a = row.get("ccy")
            if assets is None or a in out:
                out[a] = {"total": _num(row.get("cashBal"), "cashBal", True), "available": _num(row.get("availBal"), "availBal", True)}
        return out

    def one_way_mode(self):
        d = self._get("/api/v5/account/config", {})
        return bool(d) and d[0].get("posMode") == "net_mode"

    def open_positions(self, units=None):
        """{instId: signed base quantity}; `units` = {instId: base coins per contract}."""
        if self.market != "futures":
            return {}
        out = {}
        for r in self._get("/api/v5/account/positions", {"instType": "SWAP"}):
            try:
                q = float(r.get("pos") or 0)
            except (TypeError, ValueError):
                continue
            if q != 0:
                out[r["instId"]] = q * float((units or {}).get(r["instId"], 1.0))
        return out

    def set_isolated(self, ins):
        pass            # OKX: the margin mode is part of every order (tdMode) and of set-leverage

    def set_leverage(self, ins, lev):
        if self.market == "futures":
            self._post("/api/v5/account/set-leverage", {"instId": ins.symbol, "lever": str(int(lev)), "mgnMode": "isolated"})

    # ---- orders ----
    def _norm(self, d, ins, side):
        filled = float(d.get("accFillSz") or 0) * float(ins.unit)
        avg = float(d.get("avgPx") or 0)
        net = None
        if self.market == "spot" and side == "buy":
            fee = abs(float(d.get("fee") or 0))
            net = filled - (fee if d.get("feeCcy") == ins.base else 0.0)
        return {"id": d.get("ordId", ""), "filled": filled, "avg": avg, "net": net,
                "open": d.get("state") in ("live", "partially_filled"), "state": d.get("state", "")}

    def place(self, ins, side, qty, otype, price=None, tif="GTC", reduce_only=False, cl_id=None):
        ord_type = {"market": "market", "limit_ioc": "ioc", "limit_wait": "limit"}[otype]
        body = {"instId": ins.symbol, "tdMode": "cash" if self.market == "spot" else "isolated",
                "side": side, "ordType": ord_type, "sz": fmt(ins.native_qty(qty))}
        if otype != "market":
            body["px"] = fmt(price)
        if self.market == "spot" and otype == "market":
            body["tgtCcy"] = "base_ccy"
        if reduce_only and self.market == "futures":
            body["reduceOnly"] = "true"
        if cl_id:
            body["clOrdId"] = cl_id
        data = self._post("/api/v5/trade/order", body)
        row = data[0] if data else {}
        if row.get("sCode") not in (None, "0"):
            raise ApiError("OKX order rejected %s: %s" % (row.get("sCode"), row.get("sMsg")))
        if not row.get("ordId"):
            raise ApiError("OKX order reply had no order id")
        return self.get_order(ins, side, order_id=row["ordId"])

    def get_order(self, ins, side, order_id=None, cl_id=None):
        p = {"instId": ins.symbol}
        if order_id:
            p["ordId"] = order_id
        else:
            p["clOrdId"] = cl_id
        data = self._get("/api/v5/trade/order", p, urgent=True)
        if not data:
            raise ApiError("OKX returned no order")
        return self._norm(data[0], ins, side)

    def cancel(self, ins, order_id):
        try:
            self._post("/api/v5/trade/cancel-order", {"instId": ins.symbol, "ordId": order_id})
        except ApiError as e:
            if not definitely_rejected(e):
                raise
