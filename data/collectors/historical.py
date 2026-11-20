"""Historical OHLCV backfill + incremental update.

Purpose:  fill raw_candles with as much history as INDODAX serves, then keep it current.
Inputs:   IndodaxPublicClient, CandleStore, symbol, tf, lookback.
Outputs:  rows in raw_candles; a BackfillResult summary.
Method:   walk BACKWARD from now in fixed-size chunks; stop when a chunk returns no candles
          (= start of available history) or the lookback start is reached.
          Only CLOSED candles are stored (open_time + step <= now).
Unknowns (not documented): max bars per request, max history depth. The chunk size is
          configurable; truncated chunks are detected and logged, never silently accepted.
Failure:  ExchangeError propagates after client retries; already-written chunks stay committed,
          so a re-run resumes cheaply (upsert is idempotent).
Tests:    tests/unit/test_historical.py
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from data.collectors.indodax_public import IndodaxPublicClient
from data.validators.market import TF_SECONDS
from database.candle_store import CandleStore

log = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    symbol: str
    tf: str
    requests: int = 0
    written: int = 0
    reached_history_start: bool = False
    earliest: int | None = None
    latest: int | None = None


def _as_list(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    log.warning("non-list OHLC payload treated as empty", extra={"ctx": {"payload": str(payload)[:200]}})
    return []


def backfill(client: IndodaxPublicClient, store: CandleStore, symbol: str, tf: str,
             start_s: int, now_s: int | None = None, chunk_bars: int = 1000,
             empty_chunks_to_stop: int = 2) -> BackfillResult:
    step = TF_SECONDS[tf]
    now_s = now_s or int(time.time())
    end = now_s - (now_s % step)          # open time of the current (still-open) candle
    res = BackfillResult(symbol, tf)
    empties = 0
    while end > start_s:
        chunk_start = max(start_s, end - chunk_bars * step)
        candles = _as_list(client.ohlc(symbol, tf, chunk_start, end))
        res.requests += 1
        # [chunk_start, end): `end` itself was already covered by the previous (newer) chunk
        closed = [c for c in candles if isinstance(c.get("Time"), int)
                  and c["Time"] + step <= now_s and chunk_start <= c["Time"] < end]
        if closed:
            empties = 0
            res.written += store.upsert(symbol, tf, closed)
            first = min(c["Time"] for c in closed)
            # Data doesn't reach the chunk start: server truncation OR start of history OR a hole.
            # Always continue from `first` — never skip. True history start is detected by empty chunks.
            if first > chunk_start + step:
                log.debug("chunk did not reach window start; continuing from earliest received",
                         extra={"ctx": {"symbol": symbol, "tf": tf, "got": len(closed), "first": first}})
                if first >= end:  # progress guard: never loop on the same window
                    raise RuntimeError(f"backfill made no progress at {end} ({symbol} {tf})")
                end = first  # continue from what we actually got, never skip
                continue
        else:
            empties += 1
            if empties >= empty_chunks_to_stop:
                res.reached_history_start = True
                break
        end = chunk_start
    res.earliest, res.latest, _ = store.bounds(symbol, tf)
    log.info("backfill done", extra={"ctx": res.__dict__})
    return res


def update(client: IndodaxPublicClient, store: CandleStore, symbol: str, tf: str,
           now_s: int | None = None, overlap_bars: int = 3) -> BackfillResult:
    """Fetch from the last stored candle (with overlap, to repair a late-revised last bar) to now."""
    step = TF_SECONDS[tf]
    _, hi, _ = store.bounds(symbol, tf)
    if hi is None:
        raise RuntimeError(f"no data for {symbol} {tf}; run backfill first")
    return backfill(client, store, symbol, tf, start_s=hi - overlap_bars * step, now_s=now_s)
