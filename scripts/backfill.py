"""Phase 3: download historical candles into SQLite and report data quality.

Run:   python -m scripts.backfill --years 3            # full backfill (resumable)
       python -m scripts.backfill --update             # only new candles since last run
       python -m scripts.backfill --report             # quality report only, no network
Output: data/historical/candles.db  +  reports/data_quality_<UTC>.json
Exit:  0 OK · 1 quality issues found (inspect report) · 2 exchange error
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.logging_setup import setup_logging  # noqa: E402
from data.collectors.historical import backfill, update  # noqa: E402
from data.collectors.indodax_public import ExchangeError, IndodaxPublicClient  # noqa: E402
from data.validators.market import TF_SECONDS, validate_candles  # noqa: E402
from database.candle_store import CandleStore  # noqa: E402

TFS = ("15", "60", "240")


def iso(t: int | None) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M") if t else "-"


def quality(store: CandleStore, symbol: str, tf: str, now_s: int) -> dict:
    rows = store.load(symbol, tf)
    r = validate_candles(rows, tf, now_s)
    zero_vol = sum(1 for c in rows if float(c["Volume"]) == 0)
    step = TF_SECONDS[tf]
    gaps, prev = [], None
    for c in rows:
        if prev is not None and c["Time"] - prev > step:
            gaps.append({"after": iso(prev), "missing_bars": (c["Time"] - prev) // step - 1})
        prev = c["Time"]
    gaps.sort(key=lambda g: -g["missing_bars"])
    span = (rows[-1]["Time"] - rows[0]["Time"]) // step + 1 if rows else 0
    return {"rows": len(rows), "first": iso(rows[0]["Time"]) if rows else None,
            "last": iso(rows[-1]["Time"]) if rows else None,
            "coverage_pct": round(len(rows) / span * 100, 3) if span else 0,
            "missing_bars": r.metrics.get("missing"), "zero_volume_bars": zero_vol,
            "largest_gaps": gaps[:5], "ok": r.ok, "issues": [i for i in r.issues if not i.startswith("missing")][:10]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--chunk-bars", type=int, default=1000)
    ap.add_argument("--research", action="store_true", help="use research_markets from exchange.yaml")
    ap.add_argument("--tfs", default=",".join(TFS), help="comma list, e.g. 240")
    a = ap.parse_args()

    setup_logging("INFO", str(ROOT / "logs" / "backfill.log"))
    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
    client = IndodaxPublicClient.from_config(cfg)
    store = CandleStore(ROOT / "data" / "historical" / "candles.db")
    markets = cfg["research_markets"] if a.research else cfg["markets"]
    tfs = tuple(a.tfs.split(","))
    now_s = int(time.time())
    start_s = now_s - int(a.years * 365.25 * 86400)

    if not a.report:
        for m in markets:
            for tf in tfs:
                sym = m["symbol"]
                t0 = time.time()
                try:
                    _, hi, _ = store.bounds(sym, tf)
                    res = update(client, store, sym, tf, now_s) if (a.update and hi) else \
                        backfill(client, store, sym, tf, start_s, now_s, chunk_bars=a.chunk_bars)
                except ExchangeError as e:
                    print(f"[{sym} {tf}m] FAILED: {e}  (re-run to resume; check the pair exists in /api/pairs)")
                    continue
                print(f"[{sym} {tf:>3}m] {res.requests} req, {res.written} rows written, "
                      f"{iso(res.earliest)} → {iso(res.latest)}"
                      f"{'  (reached start of exchange history)' if res.reached_history_start else ''}"
                      f"  {time.time() - t0:.0f}s")

    report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "requested_years": a.years, "series": {}}
    all_ok = True
    print("\n== data quality ==")
    for m in markets:
        for tf in tfs:
            q = quality(store, m["symbol"], tf, now_s)
            report["series"][f"{m['symbol']}_{tf}"] = q
            all_ok &= q["ok"]
            print(f"{m['symbol']} {tf:>3}m: {q['rows']:>7} rows  {q['first']} → {q['last']}  "
                  f"coverage {q['coverage_pct']}%  missing {q['missing_bars']}  zero-vol {q['zero_volume_bars']}")
            for g in q["largest_gaps"][:3]:
                print(f"      gap after {g['after']}: {g['missing_bars']} bars")
            for i in q["issues"][:3]:
                print(f"      ! {i}")
    out = ROOT / "reports" / f"data_quality_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nreport: {out}")
    store.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
