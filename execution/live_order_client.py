"""INDODAX Trade API V2 client that CAN place and cancel orders (live mode only).

Purpose:  signed order placement / cancel / query on a key WITHOUT withdrawal permission.
Inputs:   INDODAX_API_KEY / INDODAX_API_SECRET (env), base URL, timeouts.
Auth:     identical HMAC-SHA256 scheme as the read client (reuses `sign`): signed params go in the
          query string, headers X-APIKEY + Sign, with timestamp + recvWindow and one clock resync.
Safety (spec 2, 3):
  - explicit (method, path) ALLOWLIST with NO withdrawal path — any other call raises before sending.
  - assert_tradable_permissions() refuses to proceed unless canTrade is true AND both canWithdraw and
    canWithdrawFiat are false (checked at startup by the runner).
  - never resends after a timeout; the broker reconciles by client order id.

!!! UNVERIFIED FIELD NAMES !!!
The POST /api/v2/order parameter names below (side/type/quantity/quoteOrderQty/price/timeInForce/
newClientOrderId) follow the Binance-compatible shape this API otherwise uses (the read client already
relies on `origClientOrderId`, `recvWindow`, `X-APIKEY`, `Sign`). They are NOT verified against a local
copy of INDODAX-TradeAPI-2-Private.md. Verify them before any real order. They are confined to
`_order_params()` and `_cancel_params()` so the fix is one place.

Tests: tests/unit/test_live_order_client.py
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import urlencode

import requests

from data.collectors.indodax_private import sign
from data.collectors.indodax_public import RETRYABLE_STATUS, ExchangeError, IndodaxPublicClient

log = logging.getLogger(__name__)

# (METHOD, path). NO withdrawal path appears here, by construction.
ALLOWED = frozenset({
    ("GET", "/api/v2/account"),
    ("GET", "/api/v2/openOrders"),
    ("GET", "/api/v2/order"),
    ("GET", "/api/v2/myTrades"),
    ("POST", "/api/v2/order"),
    ("DELETE", "/api/v2/order"),
})

VALID_SIDES = frozenset({"BUY", "SELL"})
VALID_TYPES = frozenset({"LIMIT", "MARKET"})


class PermissionUnsafe(RuntimeError):
    """Raised when the API key has withdrawal permission or lacks trade permission."""


class IndodaxTradeClient:
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base: str = "https://api.indodax.com",
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        recv_window: int = 5000,
        session: requests.Session | None = None,
        clock=time.time,
        sleep=time.sleep,
        server_time_ms=None,
    ) -> None:
        self._key = api_key if api_key is not None else os.environ.get("INDODAX_API_KEY", "")
        self._secret = api_secret if api_secret is not None else os.environ.get("INDODAX_API_SECRET", "")
        if not self._key or not self._secret:
            raise ValueError("INDODAX_API_KEY / INDODAX_API_SECRET not set (check .env)")
        self.base = base.rstrip("/")
        self.timeout, self.max_retries, self.backoff_base = timeout, max_retries, backoff_base
        self.recv_window = recv_window
        self.session = session or requests.Session()
        self._clock, self._sleep = clock, sleep
        self._server_time_ms = server_time_ms
        self._offset_ms = 0

    def __repr__(self) -> str:
        return f"IndodaxTradeClient(base={self.base!r}, key=***{self._key[-4:]})"

    def sync_clock(self) -> int:
        if self._server_time_ms:
            self._offset_ms = int(self._server_time_ms()) - int(self._clock() * 1000)
        return self._offset_ms

    def _now_ms(self) -> int:
        return int(self._clock() * 1000) + self._offset_ms

    # ---------- transport ----------
    def _request(self, method: str, path: str, params: dict | None = None) -> Any:
        if (method, path) not in ALLOWED:
            raise PermissionError(f"{method} {path!r} is not in the trade allowlist")
        resynced = False
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            q = {k: v for k, v in (params or {}).items() if v is not None}
            q["timestamp"] = self._now_ms()
            q["recvWindow"] = self.recv_window
            query = urlencode(q)
            headers = {"Accept": "application/json", "X-APIKEY": self._key, "Sign": sign(self._secret, query)}
            url = f"{self.base}{path}?{query}"
            try:
                resp = self.session.request(method, url, headers=headers, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                # A write that times out may or may not have landed. We DO NOT retry writes blindly:
                # surface the timeout so the broker reconciles by client order id first (spec 2.9).
                if method != "GET":
                    raise ExchangeError(f"network error on {method} {path}: {type(e).__name__}") from e
                last_err = ExchangeError(f"network error: {type(e).__name__}")
                log.warning("request failed", extra={"ctx": {"path": path, "attempt": attempt, "error": type(e).__name__}})
            else:
                if resp.status_code in RETRYABLE_STATUS and method == "GET":
                    last_err = ExchangeError(f"HTTP {resp.status_code}", status=resp.status_code)
                else:
                    try:
                        return IndodaxPublicClient._parse(resp, path)
                    except ExchangeError as e:
                        if e.code == -1021 and not resynced and self._server_time_ms:
                            resynced = True
                            self.sync_clock()
                            log.warning("timestamp rejected; clock re-synced", extra={"ctx": {"offset_ms": self._offset_ms}})
                            continue
                        raise
            if attempt < self.max_retries:
                self._sleep(self.backoff_base * (2 ** attempt))
        assert last_err is not None
        raise last_err

    # ---------- reads ----------
    def account(self, omit_zero: bool = True) -> dict:
        return self._request("GET", "/api/v2/account", {"omitZeroBalances": str(omit_zero).lower()})

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        return self._request("GET", "/api/v2/openOrders", {"symbol": symbol.lower()} if symbol else None)

    def get_order(self, symbol: str, client_order_id: str | None = None, order_id: str | None = None) -> dict:
        if not client_order_id and not order_id:
            raise ValueError("client_order_id or order_id required")
        p: dict = {"symbol": symbol.lower()}
        if order_id:
            p["orderId"] = order_id
        else:
            p["origClientOrderId"] = client_order_id
        return self._request("GET", "/api/v2/order", p)

    def my_trades(self, symbol: str, start_ms: int, end_ms: int, limit: int = 1000) -> list[dict]:
        body = self._request("GET", "/api/v2/myTrades",
                             {"symbol": symbol.lower(), "startTime": start_ms, "endTime": end_ms, "limit": limit})
        return body.get("data", []) if isinstance(body, dict) else body

    # ---------- writes ----------
    @staticmethod
    def _order_params(symbol, side, type_, quantity, price, quote_qty, client_order_id, time_in_force) -> dict:
        # !!! VERIFY field names against INDODAX-TradeAPI-2-Private.md before live (see module header). !!!
        p: dict = {"symbol": symbol.lower(), "side": side, "type": type_, "newClientOrderId": client_order_id}
        if type_ == "LIMIT":
            p["timeInForce"] = time_in_force
            p["price"] = price
            p["quantity"] = quantity
        elif side == "BUY":          # MARKET BUY is sized in IDR (quote)
            p["quoteOrderQty"] = quote_qty
        else:                        # MARKET SELL is sized in coin (base)
            p["quantity"] = quantity
        return p

    def create_order(self, symbol: str, side: str, type_: str, *, client_order_id: str,
                     quantity: float | None = None, price: float | None = None,
                     quote_qty: float | None = None, time_in_force: str = "GTC") -> dict:
        if side not in VALID_SIDES or type_ not in VALID_TYPES:
            raise ValueError(f"bad side/type: {side}/{type_}")
        if type_ == "LIMIT" and (price is None or quantity is None):
            raise ValueError("LIMIT needs price and quantity")
        if type_ == "MARKET" and side == "BUY" and quote_qty is None:
            raise ValueError("MARKET BUY needs quote_qty (IDR)")
        if type_ == "MARKET" and side == "SELL" and quantity is None:
            raise ValueError("MARKET SELL needs quantity (coin)")
        params = self._order_params(symbol, side, type_, quantity, price, quote_qty, client_order_id, time_in_force)
        return self._request("POST", "/api/v2/order", params)

    def cancel_order(self, symbol: str, client_order_id: str | None = None, order_id: str | None = None) -> dict:
        if not client_order_id and not order_id:
            raise ValueError("client_order_id or order_id required")
        p: dict = {"symbol": symbol.lower()}
        if order_id:
            p["orderId"] = order_id
        else:
            p["origClientOrderId"] = client_order_id
        return self._request("DELETE", "/api/v2/order", p)

    # ---------- safety ----------
    def assert_tradable_permissions(self) -> dict:
        """Spec 2.2/3.2: refuse unless canTrade and NOT canWithdraw/canWithdrawFiat. Returns account."""
        acct = self.account()
        for flag in ("canWithdraw", "canWithdrawFiat"):
            if flag not in acct:
                raise PermissionUnsafe(f"'{flag}' missing from account — cannot verify withdrawal is off")
            if acct[flag] is not False:
                raise PermissionUnsafe(f"{flag}={acct[flag]} — WITHDRAWAL PERMISSION ENABLED; revoke this key")
        if acct.get("canTrade") is not True:
            raise PermissionUnsafe(f"canTrade={acct.get('canTrade')} — key cannot trade")
        return acct
