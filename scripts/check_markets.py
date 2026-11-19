"""Milestone 1: read-only market check for BTC/IDR and ETH/IDR.

Purpose:  fetch live public data, validate it, and write an exact JSON record of what came back.
Inputs:   config/exchange.yaml. No API key. No orders.
Outputs:  stdout summary + reports/market_check_<UTC>.json (raw payloads + validation).
Exit codes: 0 = all markets valid, 1 = at least one market disabled, 2 = could not reach exchange.
Run:      python -m scripts.check_markets
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.logging_setup import setup_logging  # noqa: E402
from data.collectors.indodax_public import ExchangeError, IndodaxPublicClient  # noqa: E402
from data.validators import market as v  # noqa: E402

log = logging.getLogger("check_markets")


def run() -> int:
    setup_logging("INFO", str(ROOT / "logs" / "check_markets.log"))
    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
    dq = cfg["data_quality"]
    client = IndodaxPublicClient.from_config(cfg)
    report: dict = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "markets": {}}

    try:
        st = client.server_time()
        clock = v.check_clock(int(st["server_time"]), time.time())
        pairs = client.pairs()
        increments = client.price_increments().get("increments", {})
    except (ExchangeError, KeyError, TypeError, ValueError) as e:
        log.error("exchange unreachable or malformed", extra={"ctx": {"error": str(e)}})
        print(f"FAILED: cannot fetch exchange metadata: {e}")
        return 2
    report["server_time"] = st
    report["clock"] = clock.__dict__

    all_ok = clock.ok
    now_s = int(time.time())
    for m in cfg["markets"]:
        sym, pid = m["symbol"], m["pair_id"]
        entry: dict = {"raw": {}, "checks": {}}
        try:
            pair = v.find_pair(pairs, pid)
            entry["raw"]["pair"] = pair
            entry["checks"]["metadata"] = v.validate_pair_metadata(pair, pid).__dict__

            ticker_id = pair.get("ticker_id") if pair else None
            entry["raw"]["tick_size"] = increments.get(ticker_id)

            book = client.book_ticker(sym)
            depth = client.depth(sym, dq["depth_levels"])
            entry["raw"]["book_ticker"] = book
            entry["raw"]["depth_top3"] = {"bids": depth.get("bids", [])[:3], "asks": depth.get("asks", [])[:3]}
            entry["checks"]["book"] = v.validate_book(book, depth, dq["max_spread_pct"]).__dict__

            for tf, n in (("15", 300), ("60", 300), ("240", 300)):
                step = v.TF_SECONDS[tf]
                candles = client.ohlc(sym, tf, now_s - n * step, now_s)
                entry["raw"][f"ohlc_{tf}_last2"] = candles[-2:] if isinstance(candles, list) else candles
                entry["checks"][f"ohlc_{tf}"] = v.validate_candles(candles if isinstance(candles, list) else [], tf, now_s).__dict__
        except ExchangeError as e:
            entry["checks"]["fetch"] = {"ok": False, "issues": [str(e)], "metrics": {}}

        entry["tradable"] = all(c["ok"] for c in entry["checks"].values())
        all_ok &= entry["tradable"]
        report["markets"][sym] = entry

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"market_check_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(report, indent=2, default=str))

    print(f"clock skew: {clock.metrics['clock_skew_s']}s  {'OK' if clock.ok else 'FAIL ' + '; '.join(clock.issues)}")
    for sym, e in report["markets"].items():
        meta = e["checks"].get("metadata", {}).get("metrics", {})
        book = e["checks"].get("book", {}).get("metrics", {})
        print(f"\n[{sym}] {'ENABLED' if e['tradable'] else 'DISABLED'}")
        if book:
            print(f"  bid {book.get('bid')}  ask {book.get('ask')}  spread {book.get('spread_pct')}%  "
                  f"depth bid/ask IDR {book.get('bid_depth_idr'):,}/{book.get('ask_depth_idr'):,}")
        if meta:
            print(f"  min order IDR {meta.get('trade_min_base_currency')}  min qty {meta.get('trade_min_traded_currency')}  "
                  f"fee maker/taker {meta.get('trade_fee_percent_maker')}%/{meta.get('trade_fee_percent_taker')}%  "
                  f"tick {e['raw'].get('tick_size')}")
        for name, c in e["checks"].items():
            if name.startswith("ohlc"):
                mt = c["metrics"]
                print(f"  {name}: {mt.get('closed', 0)} closed candles, missing {mt.get('missing', '?')}  "
                      f"{'OK' if c['ok'] else 'FAIL'}")
            if c["issues"]:
                for i in c["issues"][:5]:
                    print(f"    ! {name}: {i}")
    print(f"\nreport: {out}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(run())
