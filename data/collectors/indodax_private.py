"""Read-only INDODAX Trade API V2 private client (Phase 2).

Purpose:  read account balances, API-key permissions, open orders, order history and fills.
Inputs:   INDODAX_API_KEY / INDODAX_API_SECRET from environment (.env), config/exchange.yaml.
Outputs:  parsed JSON exactly as returned by the exchange.
Auth (docs: INDODAX-TradeAPI-2-Private.md, "SIGNED Endpoint Examples"):
  query = urlencode(params + timestamp + recvWindow)
  Sign  = HMAC-SHA256(secret, query).hexdigest()   -> header "Sign"; key -> header "X-APIKEY"
Endpoints used (all GET):
  /api/v2/account            permissions + balances
  /api/v2/openOrders         pending orders
  /api/v2/order              single order (needed later for timeout reconciliation)
  /api/v2/order/histories    order history, max 7-day window
  /api/v2/myTrades           fills incl. commission (fee + tax), max 7-day window
Failure conditions:
  missing credentials -> ValueError at construction (no request sent)
  network/5xx/429     -> retried with backoff, then ExchangeError
  -1021 timestamp     -> clock offset re-synced from server once, then ExchangeError
  -1002/-2015 auth    -> ExchangeError, no retry
Safety: an explicit allowlist of GET paths. Any other path or method raises before sending.
        This module cannot create/cancel orders or withdraw.
Tests: tests/unit/test_private_client.py
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any
from urllib.parse import urlencode

import requests

from data.collectors.indodax_public import RETRYABLE_STATUS, ExchangeError, IndodaxPublicClient

log = logging.getLogger(__name__)

READ_ONLY_PATHS = frozenset({
    "/api/v2/account",
    "/api/v2/openOrders",
    "/api/v2/order",
    "/api/v2/order/histories",
    "/api/v2/myTrades",
})
MAX_WINDOW_MS = 7 * 24 * 3600 * 1000


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class IndodaxPrivateReadClient:
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
        self._server_time_ms = server_time_ms  # callable returning exchange ms; used to fix skew
        self._offset_ms = 0

    def __repr__(self) -> str:  # never leak credentials via repr/logging
        return f"IndodaxPrivateReadClient(base={self.base!r}, key=***{self._key[-4:]})"

    def sync_clock(self) -> int:
        if self._server_time_ms:
            self._offset_ms = int(self._server_time_ms()) - int(self._clock() * 1000)
        return self._offset_ms

    def _now_ms(self) -> int:
        return int(self._clock() * 1000) + self._offset_ms

    def _get(self, path: str, params: dict | None = None) -> Any:
        if path not in READ_ONLY_PATHS:
            raise PermissionError(f"path {path!r} is not in the read-only allowlist")
        resynced = False
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            q = dict(params or {})
            q["timestamp"] = self._now_ms()
            q["recvWindow"] = self.recv_window
            query = urlencode(q)
            headers = {"Accept": "application/json", "X-APIKEY": self._key, "Sign": sign(self._secret, query)}
            try:
                resp = self.session.get(f"{self.base}{path}?{query}", headers=headers, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = ExchangeError(f"network error: {type(e).__name__}")
                log.warning("private GET failed", extra={"ctx": {"path": path, "attempt": attempt, "error": type(e).__name__}})
            else:
                if resp.status_code in RETRYABLE_STATUS:
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

    # ---------- endpoints ----------
    def account(self, omit_zero: bool = True) -> dict:
        return self._get("/api/v2/account", {"omitZeroBalances": str(omit_zero).lower()})

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        return self._get("/api/v2/openOrders", {"symbol": symbol} if symbol else None)

    def get_order(self, symbol: str, order_id: str | None = None, client_order_id: str | None = None) -> dict:
        if not order_id and not client_order_id:
            raise ValueError("order_id or client_order_id required")
        p: dict = {"symbol": symbol}
        if order_id:
            p["orderId"] = order_id
        else:
            p["origClientOrderId"] = client_order_id
        return self._get("/api/v2/order", p)

    def _windowed(self, path: str, symbol: str, start_ms: int, end_ms: int, limit: int) -> list[dict]:
        """Walks [start, end] in <=7-day chunks (documented max range)."""
        out: list[dict] = []
        s = start_ms
        while s < end_ms:
            e = min(s + MAX_WINDOW_MS - 1, end_ms)
            body = self._get(path, {"symbol": symbol, "startTime": s, "endTime": e, "limit": limit, "sort": "asc"})
            rows = body.get("data", []) if isinstance(body, dict) else body
            if len(rows) >= limit:
                log.warning("page full; results may be truncated", extra={"ctx": {"path": path, "symbol": symbol, "start": s}})
            out.extend(rows)
            s = e + 1
        return out

    def order_history(self, symbol: str, start_ms: int, end_ms: int, limit: int = 1000) -> list[dict]:
        return self._windowed("/api/v2/order/histories", symbol.lower(), start_ms, end_ms, limit)

    def my_trades(self, symbol: str, start_ms: int, end_ms: int, limit: int = 1000) -> list[dict]:
        return self._windowed("/api/v2/myTrades", symbol.lower(), start_ms, end_ms, limit)


def check_permissions(acct: dict, allow_trade: bool) -> list[str]:
    """Spec 3.2: withdrawal must be disabled. Returns list of violations (empty = OK)."""
    v = []
    for flag in ("canWithdraw", "canWithdrawFiat"):
        if flag not in acct:
            v.append(f"'{flag}' missing from account response — cannot verify withdrawal is disabled")
        elif acct[flag] is not False:
            v.append(f"{flag}={acct[flag]} — WITHDRAWAL PERMISSION IS ENABLED")
    if not allow_trade and acct.get("canTrade") is not False:
        v.append(f"canTrade={acct.get('canTrade')} — trading permission enabled during read-only phase")
    return v
