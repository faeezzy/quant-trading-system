"""Deterministic in-memory stand-in for IndodaxTradeClient — tests only, never imported in live code.

It mirrors the client's interface (account / open_orders / get_order / my_trades / create_order /
cancel_order) and an in-memory book, so LiveBroker and the runner can be exercised with no network
and with injectable faults: timeouts that LOSE the order, timeouts that LAND it anyway, partial
fills, and rejects. Order/trade dict shapes follow the Binance-compatible fields the broker reads;
the real field names are unverified (see live_order_client header) and parsing is isolated in
LiveBroker so one fix covers both.
"""
from __future__ import annotations

from data.collectors.indodax_public import ExchangeError


class FakeExchange:
    def __init__(self, bid: dict[str, float], ask: dict[str, float],
                 balances: dict[str, float] | None = None, fee_pct: float = 0.2):
        self.bid = dict(bid)
        self.ask = dict(ask)
        self.fee_pct = fee_pct
        self.bal = {"IDR": 1_000_000.0}
        if balances:
            self.bal.update(balances)
        self.orders: dict[str, dict] = {}   # clientOrderId -> order
        self.trades: list[dict] = []
        self._oid = 0
        self.can_trade = True
        self.can_withdraw = False
        self.can_withdraw_fiat = False
        # fault injection (consumed once)
        self.fail_next_create: str | None = None   # "lost" (not placed) or "landed" (placed+filled, raise)
        self.partial_ratio: float | None = None     # 0<r<1 -> next order fills only this fraction
        self.reject_next = False

    # ---------- test helpers ----------
    def set_touch(self, symbol: str, bid: float, ask: float) -> None:
        self.bid[symbol], self.ask[symbol] = bid, ask

    def _coin(self, symbol: str) -> str:
        return symbol[:-3] if symbol.upper().endswith("IDR") else symbol

    # ---------- reads ----------
    def account(self, omit_zero: bool = True) -> dict:
        balances = [{"asset": a, "free": str(v), "locked": "0"} for a, v in self.bal.items() if v or not omit_zero]
        return {"canTrade": self.can_trade, "canWithdraw": self.can_withdraw,
                "canWithdrawFiat": self.can_withdraw_fiat, "balances": balances}

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        return [o for o in self.orders.values() if o["status"] in ("NEW", "PARTIALLY_FILLED")
                and (symbol is None or o["symbol"] == symbol.lower())]

    def get_order(self, symbol: str, client_order_id: str | None = None, order_id: str | None = None) -> dict:
        for o in self.orders.values():
            if (client_order_id and o["clientOrderId"] == client_order_id) or (order_id and o["orderId"] == order_id):
                return dict(o)
        raise ExchangeError("order not found", code=-2013)

    def my_trades(self, symbol: str, start_ms: int, end_ms: int, limit: int = 1000) -> list[dict]:
        return [t for t in self.trades if t["symbol"] == symbol.lower() and start_ms <= t["time"] <= end_ms]

    # ---------- writes ----------
    def _fill(self, symbol: str, side: str, qty: float, price: float, client_order_id: str, order_id: str) -> dict:
        coin = self._coin(symbol)
        notional = qty * price
        fee = notional * self.fee_pct / 100
        if side == "BUY":
            self.bal["IDR"] -= notional + fee
            self.bal[coin] = self.bal.get(coin, 0.0) + qty
        else:
            self.bal["IDR"] += notional - fee
            self.bal[coin] = self.bal.get(coin, 0.0) - qty
        self.trades.append({"symbol": symbol.lower(), "orderId": order_id, "clientOrderId": client_order_id,
                            "side": side, "price": str(price), "qty": str(qty), "quoteQty": str(notional),
                            "commission": str(fee), "commissionAsset": "IDR", "isMaker": False, "time": 1})
        return {"price": price, "qty": qty, "notional": notional, "fee": fee}

    def create_order(self, symbol: str, side: str, type_: str, *, client_order_id: str,
                     quantity: float | None = None, price: float | None = None,
                     quote_qty: float | None = None, time_in_force: str = "GTC") -> dict:
        self._oid += 1
        order_id = f"F{self._oid}"
        if self.reject_next:
            self.reject_next = False
            self.orders[client_order_id] = {"symbol": symbol.lower(), "orderId": order_id,
                                            "clientOrderId": client_order_id, "side": side, "type": type_,
                                            "status": "REJECTED", "origQty": str(quantity or 0),
                                            "executedQty": "0", "price": str(price or 0)}
            raise ExchangeError("order rejected", code=-2010)

        touch = self.ask[symbol] if side == "BUY" else self.bid[symbol]
        if type_ == "MARKET":
            fill_price = touch
        else:  # LIMIT: marketable only if it crosses the touch
            marketable = (side == "BUY" and price >= touch) or (side == "SELL" and price <= touch)
            fill_price = touch if marketable else None

        # intended quantity (coin). MARKET BUY is sized in IDR -> convert at fill price.
        want_qty = quantity
        if type_ == "MARKET" and side == "BUY":
            want_qty = (quote_qty / fill_price) if fill_price else 0.0

        landed = self.fail_next_create == "landed"
        lost = self.fail_next_create == "lost"
        self.fail_next_create = None

        if lost:
            raise ExchangeError("network error on POST /api/v2/order: Timeout")

        exec_qty = 0.0
        status = "NEW"
        if fill_price is not None and want_qty and want_qty > 0:
            ratio = self.partial_ratio if self.partial_ratio is not None else 1.0
            self.partial_ratio = None
            exec_qty = want_qty * ratio
            self._fill(symbol, side, exec_qty, fill_price, client_order_id, order_id)
            status = "FILLED" if ratio >= 1.0 else "PARTIALLY_FILLED"

        order = {"symbol": symbol.lower(), "orderId": order_id, "clientOrderId": client_order_id,
                 "side": side, "type": type_, "status": status, "origQty": str(want_qty or 0),
                 "executedQty": str(exec_qty),
                 "cummulativeQuoteQty": str(exec_qty * (fill_price or 0)),
                 "price": str(price or fill_price or 0)}
        self.orders[client_order_id] = order
        if landed:
            # Server accepted & filled, but the client saw a timeout: broker must discover via reconcile.
            raise ExchangeError("network error on POST /api/v2/order: Timeout")
        return dict(order)

    def cancel_order(self, symbol: str, client_order_id: str | None = None, order_id: str | None = None) -> dict:
        for o in self.orders.values():
            if (client_order_id and o["clientOrderId"] == client_order_id) or (order_id and o["orderId"] == order_id):
                if o["status"] in ("NEW", "PARTIALLY_FILLED"):
                    o["status"] = "CANCELED"
                return dict(o)
        raise ExchangeError("order not found", code=-2013)
