"""One-shot research data pull (run on the owner's machine; the analysis sandbox has no exchange access).

1) Crypto Fear & Greed Index, full daily history (alternative.me).
2) INDODAX 4H candles for research_markets, as far back as INDODAX serves (default lookback 8 years;
   the backfill stops by itself when the exchange returns no older data).
3) Writes ~/research_data.db.gz for upload.

Run:  python -m scripts.fetch_research_data [--years 8]
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.logging_setup import setup_logging  # noqa: E402
from data.collectors import sentiment  # noqa: E402
from data.collectors.historical import backfill  # noqa: E402
from data.collectors.indodax_public import ExchangeError, IndodaxPublicClient  # noqa: E402
from database.candle_store import CandleStore  # noqa: E402


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") if t else "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=8.0)
    a = ap.parse_args()
    setup_logging("WARNING", str(ROOT / "logs" / "fetch_research_data.log"))
    db = ROOT / "data" / "historical" / "candles.db"
    store = CandleStore(db)

    print("== sentiment: Crypto Fear & Greed Index (alternative.me) ==")
    try:
        rows, bad = sentiment.parse(sentiment.fetch())
        n = sentiment.store(store.conn, rows)
        print(f"  {n} days stored ({iso(min(r[0] for r in rows))} → {iso(max(r[0] for r in rows))}), {bad} malformed rows skipped")
    except Exception as e:  # report, continue with candles
        print(f"  FAILED: {type(e).__name__}: {e}")

    print("\n== INDODAX 4H candles ==")
    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
    client = IndodaxPublicClient.from_config(cfg)
    now = int(time.time())
    start = now - int(a.years * 365.25 * 86400)
    for m in cfg["research_markets"]:
        sym = m["symbol"]
        try:
            r = backfill(client, store, sym, "240", start, now, chunk_bars=1000)
            print(f"  {sym:<8} {iso(r.earliest)} → {iso(r.latest)}  rows+{r.written}"
                  f"{'  (start of INDODAX history)' if r.reached_history_start else ''}")
        except ExchangeError as e:
            print(f"  {sym:<8} FAILED: {e}")
    store.close()

    con = sqlite3.connect(db)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    out = Path.home() / "research_data.db.gz"
    with open(db, "rb") as src, gzip.open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    print(f"\nupload this file to the chat: {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
