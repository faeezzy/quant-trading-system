"""Market-data validation (spec Part 4.3 and 3.5).

Purpose:  decide whether fetched data is trustworthy enough to use. Pure functions, no I/O.
Inputs:   raw exchange payloads + thresholds from config/exchange.yaml.
Outputs:  ValidationResult(ok, issues, metrics). ok=False => NO TRADE.
Failure:  never raises on bad data; bad data becomes an issue string.
Tests:    tests/unit/test_validators.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

TF_SECONDS = {"1": 60, "15": 900, "30": 1800, "60": 3600, "240": 14400, "1D": 86400}


@dataclass
class ValidationResult:
    ok: bool = True
    issues: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.issues.append(msg)


def _dec(x: Any) -> Decimal | None:
    try:
        d = Decimal(str(x))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def find_pair(pairs: list[dict], pair_id: str) -> dict | None:
    return next((p for p in pairs if p.get("id") == pair_id), None)


def validate_pair_metadata(pair: dict | None, pair_id: str) -> ValidationResult:
    """Spec 3.5: if required metadata is missing, the market is disabled."""
    r = ValidationResult()
    if pair is None:
        r.fail(f"{pair_id}: not listed in /api/pairs")
        return r
    required = ["trade_min_base_currency", "trade_min_traded_currency", "price_precision",
                "quantity_increment", "trade_fee_percent_taker", "trade_fee_percent_maker"]
    for k in required:
        if k not in pair:
            r.fail(f"{pair_id}: metadata field '{k}' missing")
        else:
            r.metrics[k] = pair[k]
    if pair.get("is_maintenance") not in (None, 0, False):
        r.fail(f"{pair_id}: market under maintenance")
    if pair.get("is_market_suspended") not in (None, 0, False):
        r.fail(f"{pair_id}: market suspended")
    return r


def validate_book(book: dict, depth: dict, max_spread_pct: float) -> ValidationResult:
    r = ValidationResult()
    bid, ask = _dec(book.get("bidPrice")), _dec(book.get("askPrice"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        r.fail(f"invalid bid/ask: bid={book.get('bidPrice')} ask={book.get('askPrice')}")
        return r
    if ask <= bid:
        r.fail(f"crossed/locked book: bid={bid} ask={ask}")
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100
    r.metrics.update(bid=str(bid), ask=str(ask), mid=str(mid), spread_pct=round(float(spread_pct), 5))
    if spread_pct > Decimal(str(max_spread_pct)):
        r.fail(f"spread {spread_pct:.4f}% > max {max_spread_pct}%")

    bids, asks = depth.get("bids") or [], depth.get("asks") or []
    if not bids or not asks:
        r.fail("empty order book side")
        return r
    try:
        bid_prices = [Decimal(str(p)) for p, _ in bids]
        ask_prices = [Decimal(str(p)) for p, _ in asks]
        bid_idr = sum(Decimal(str(p)) * Decimal(str(q)) for p, q in bids)
        ask_idr = sum(Decimal(str(p)) * Decimal(str(q)) for p, q in asks)
    except (InvalidOperation, ValueError, TypeError):
        r.fail("unparseable depth levels")
        return r
    if bid_prices != sorted(bid_prices, reverse=True) or ask_prices != sorted(ask_prices):
        r.fail("depth levels not sorted as documented")
    r.metrics.update(depth_levels=min(len(bids), len(asks)),
                     bid_depth_idr=int(bid_idr), ask_depth_idr=int(ask_idr))
    return r


def validate_candles(candles: list[dict], tf: str, now_s: int) -> ValidationResult:
    """Checks timestamps, gaps, duplicates, OHLC sanity, volume; drops the still-open candle."""
    r = ValidationResult()
    step = TF_SECONDS[tf]
    if not candles:
        r.fail("no candles returned")
        return r
    times: list[int] = []
    for c in candles:
        t = c.get("Time")
        o, h, l, cl, v = (_dec(c.get(k)) for k in ("Open", "High", "Low", "Close", "Volume"))
        if not isinstance(t, int) or None in (o, h, l, cl, v):
            r.fail(f"malformed candle: {c}")
            continue
        if min(o, h, l, cl) <= 0:
            r.fail(f"non-positive price at {t}")
        if h < max(o, cl, l) or l > min(o, cl, h):
            r.fail(f"impossible OHLC at {t}: O={o} H={h} L={l} C={cl}")
        if v < 0:
            r.fail(f"negative volume at {t}")
        if t % step != 0:
            r.fail(f"timestamp {t} not aligned to {tf}m grid")
        times.append(t)
    if len(times) != len(set(times)):
        r.fail(f"duplicate timestamps: {len(times) - len(set(times))}")
    if times != sorted(times):
        r.fail("candles not in chronological order")
    uniq = sorted(set(times))
    missing = sum((b - a) // step - 1 for a, b in zip(uniq, uniq[1:]) if b - a > step)
    if missing:
        r.fail(f"missing candles: {missing}")
    closed = [t for t in uniq if t + step <= now_s]
    last_closed = closed[-1] if closed else None
    r.metrics.update(count=len(uniq), closed=len(closed), missing=missing,
                     last_closed_open_time=last_closed,
                     open_candle_present=bool(uniq and uniq[-1] + step > now_s))
    # stale: newest closed candle should end within ~2 intervals of now
    if last_closed is None or now_s - (last_closed + step) > 2 * step:
        r.fail(f"stale candles: last closed candle opened at {last_closed}")
    return r


def check_clock(server_time_ms: int, local_time_s: float, max_skew_s: float = 5.0) -> ValidationResult:
    r = ValidationResult()
    skew = local_time_s - server_time_ms / 1000
    r.metrics["clock_skew_s"] = round(skew, 3)
    if abs(skew) > max_skew_s:
        r.fail(f"local clock skew {skew:.2f}s exceeds {max_skew_s}s — fix NTP before trading")
    return r
