"""Read-only INDODAX public market-data client.

Purpose:   fetch public market data (no API key, no orders, no private endpoints).
Inputs:    config/exchange.yaml (base URLs, timeouts, retries, throttle).
Outputs:   parsed JSON (dict/list) exactly as returned by the exchange.
Endpoints (all documented in btcid/indodax-official-api-docs):
  legacy  GET /api/server_time
  legacy  GET /api/pairs                       -> min order, precision, fees, maintenance flags
  legacy  GET /api/price_increments            -> tick size
  legacy  GET /tradingview/history_v2          -> OHLCV candles
  v2      GET /api/v2/ticker/bookTicker        -> best bid/ask
  v2      GET /api/v2/depth                    -> order book
Failure conditions:
  - network error / timeout / 5xx / 429  -> retried with exponential backoff, then ExchangeError
  - 4xx (non-429) or INDODAX error body {"code","msg"} -> ExchangeError, no retry
  - non-JSON body -> ExchangeError
Safety: GET only. This module has no code path that can place, cancel or withdraw.
Tests: tests/unit/test_public_client.py
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ExchangeError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code


class IndodaxPublicClient:
    def __init__(
        self,
        legacy_base: str = "https://indodax.com",
        v2_base: str = "https://api.indodax.com",
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        min_interval: float = 0.5,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ) -> None:
        self.legacy_base = legacy_base.rstrip("/")
        self.v2_base = v2_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.min_interval = min_interval
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "indodax-quant/0.1 (read-only)"})
        self._sleep = sleep
        self._last_call = 0.0

    @classmethod
    def from_config(cls, cfg: dict) -> "IndodaxPublicClient":
        ep, http = cfg["endpoints"], cfg["http"]
        return cls(
            legacy_base=ep["legacy_public_base"],
            v2_base=ep["tapi_v2_public_base"],
            timeout=http["timeout_seconds"],
            max_retries=http["max_retries"],
            backoff_base=http["backoff_base_seconds"],
            min_interval=http["min_interval_seconds"],
        )

    # ---------- transport ----------
    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    def _get(self, url: str, params: dict | None = None) -> Any:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = ExchangeError(f"network error: {type(e).__name__}: {e}")
                log.warning("request failed", extra={"ctx": {"url": url, "attempt": attempt, "error": str(e)}})
            else:
                if resp.status_code in RETRYABLE_STATUS:
                    last_err = ExchangeError(f"HTTP {resp.status_code}", status=resp.status_code)
                    log.warning("retryable status", extra={"ctx": {"url": url, "status": resp.status_code, "attempt": attempt}})
                else:
                    return self._parse(resp, url)
            if attempt < self.max_retries:
                self._sleep(self.backoff_base * (2 ** attempt))
        assert last_err is not None
        raise last_err

    @staticmethod
    def _parse(resp: requests.Response, url: str) -> Any:
        try:
            body = resp.json()
        except ValueError:
            raise ExchangeError(f"non-JSON response from {url} (HTTP {resp.status_code}): {resp.text[:200]!r}",
                                status=resp.status_code)
        if resp.status_code >= 400:
            msg = body.get("msg") if isinstance(body, dict) else None
            raise ExchangeError(f"HTTP {resp.status_code}: {msg or body}", status=resp.status_code,
                                code=body.get("code") if isinstance(body, dict) else None)
        # V2 error envelope: {"code": -1121, "msg": "Invalid symbol."}
        if isinstance(body, dict) and "code" in body and "msg" in body and isinstance(body["code"], int) and body["code"] < 0:
            raise ExchangeError(f"exchange error {body['code']}: {body['msg']}", status=resp.status_code, code=body["code"])
        # legacy error envelope: {"error": "...", "error_description": "..."}
        if isinstance(body, dict) and "error" in body and len(body) <= 3:
            raise ExchangeError(f"exchange error: {body.get('error')} {body.get('error_description', '')}".strip(),
                                status=resp.status_code)
        return body

    # ---------- endpoints ----------
    def server_time(self) -> dict:
        return self._get(f"{self.legacy_base}/api/server_time")

    def pairs(self) -> list[dict]:
        return self._get(f"{self.legacy_base}/api/pairs")

    def price_increments(self) -> dict:
        return self._get(f"{self.legacy_base}/api/price_increments")

    def book_ticker(self, symbol: str) -> dict:
        return self._get(f"{self.v2_base}/api/v2/ticker/bookTicker", {"symbol": symbol})

    def depth(self, symbol: str, limit: int = 20) -> dict:
        return self._get(f"{self.v2_base}/api/v2/depth", {"symbol": symbol, "limit": limit})

    def ohlc(self, symbol: str, tf: str, start: int, end: int) -> list[dict]:
        """tf: '1','15','30','60','240','1D','3D','1W'. start/end: unix seconds."""
        return self._get(f"{self.legacy_base}/tradingview/history_v2",
                         {"symbol": symbol, "tf": tf, "from": start, "to": end})
