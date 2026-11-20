"""Paper trading runner — D2 weekly momentum rotation on BTC/ETH (status: PROMISING, UNVALIDATED LIVE).

Run every hour (cron), e.g.:   5 * * * *  cd ~/quant-trading-system && .venv/bin/python -m scripts.paper_trade
Each run:
  1. single-instance lock (spec 16) — a second copy exits immediately
  2. refuse unless TRADING_MODE=paper (this script has NO private/order code at all)
  3. update 4H candles (public API) and validate live books (spread, crossed book)
  4. synthetic stop check against live bid (every run)
  5. once per completed UTC day: evaluate D2 on the last COMPLETE daily bar and act (idempotent:
     the decided day is persisted, so re-runs/restarts never double-trade)
  6. mark equity, drawdown guard, append a line to reports/paper_daily.csv
Strategy params are the pre-declared ones (lookback 28, SMA100, stop 3x daily ATR) — do not tune.
"""
from __future__ import annotations

import csv
import fcntl
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.logging_setup import setup_logging  # noqa: E402
from data.collectors.historical import backfill, update  # noqa: E402
from data.collectors.indodax_public import ExchangeError, IndodaxPublicClient  # noqa: E402
from data.validators.market import validate_book  # noqa: E402
from database.candle_store import CandleStore  # noqa: E402
from execution.paper_broker import PaperBroker, PaperConfig  # noqa: E402
from scripts.scan_signals import load  # noqa: E402
from strategies import v3  # noqa: E402

SYMBOLS = ["BTCIDR", "ETHIDR"]
WARMUP_DAYS = 260


def decide(frames: dict[str, pd.DataFrame], held: str | None) -> tuple[str | None, int, dict]:
    """Return (target symbol or None, day_ts of the last complete daily bar, atr by symbol)."""
    last_day = min(int(f["open_time"].iloc[-1]) for f in frames.values())
    rows = {s: f[f["open_time"] == last_day].iloc[0] for s, f in frames.items()}
    atrs = {s: float(r["atr"]) for s, r in rows.items()}
    if held and bool(rows[held]["exit_signal"]):
        held = None
    if held:
        return held, last_day, atrs
    for s in frames:
        if bool(rows[s]["signal"]):
            return s, last_day, atrs
    return None, last_day, atrs


def main() -> int:
    load_dotenv(ROOT / ".env")
    if os.environ.get("TRADING_MODE", "paper") != "paper":
        print("refusing: TRADING_MODE is not 'paper'"); return 3
    lock = open(ROOT / "paper_trade.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another paper_trade instance is running; exiting"); return 0
    setup_logging("WARNING", str(ROOT / "logs" / "paper_trade.log"))
    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
    risk = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text())
    client = IndodaxPublicClient.from_config(cfg)
    store = CandleStore(ROOT / "data" / "historical" / "candles.db")
    broker = PaperBroker(ROOT / "data" / "paper.db", PaperConfig(
        initial_equity=risk["initial_capital_idr"], max_allocation=risk["max_active_allocation_idr"],
        risk_pct=risk["risk_per_trade_pct"], max_dd_pct_initial=risk["max_drawdown_pct_of_initial"]))
    now = int(time.time())
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 3) data + live books (NO VERIFIED DATA = NO TRADE)
    books, bid, ask = {}, {}, {}
    try:
        for s in SYMBOLS:
            hi = store.bounds(s, "240")[1]
            if hi is None or now - hi > 3 * 86400:
                backfill(client, store, s, "240", now - WARMUP_DAYS * 86400, now)
            else:
                update(client, store, s, "240", now)
            b = client.book_ticker(s)
            d = client.depth(s, 20)
            chk = validate_book(b, d, cfg["data_quality"]["max_spread_pct"])
            if not chk.ok:
                raise ExchangeError(f"{s} book invalid: {chk.issues}")
            books[s] = chk.metrics
            bid[s], ask[s] = float(b["bidPrice"]), float(b["askPrice"])
    except ExchangeError as e:
        broker.event("ERROR", f"data unavailable, no action: {e}")
        print(f"{ts} NO ACTION — data unavailable: {e}")
        return 2

    pos = broker.position
    # 4) synthetic stop (local, not exchange-held)
    if pos and bid[pos["symbol"]] <= pos["stop"]:
        res = broker.sell(bid[pos["symbol"]], "stop")
        broker.event("INFO", f"STOP {res}")
        print(f"{ts} STOP hit: {res}")
        pos = None

    # 5) daily decision, once per completed day
    h4 = {s: load(store, s, "240") for s in SYMBOLS}
    h4 = {s: d[d["open_time"] >= now - WARMUP_DAYS * 86400].reset_index(drop=True) for s, d in h4.items()}
    frames = v3.weekly_rotation(h4)
    target, day, atrs = decide(frames, pos["symbol"] if pos else None)
    action = "none"
    if broker.state["last_decision_day"] != day:
        action = "hold" if pos and target == pos["symbol"] else ("stay in IDR" if not target else "")
        if pos and target != pos["symbol"]:
            r = broker.sell(bid[pos["symbol"]], "rotation_exit")
            action = f"SELL {r['symbol']} pnl {r['pnl']:,.0f} ({r['r']:+.2f}R)"
            pos = None
        if pos is None and target:
            p = broker.buy(target, ask[target], atrs[target], "rotation_entry")
            sep = " | " if action else ""
            action += (f"{sep}BUY {target} qty {p['qty']:.8f} @ {p['entry']:,.0f} stop {p['stop']:,.0f}" if p
                       else f"{sep}buy {target} skipped (see paper_events)")
        broker.set_last_decision_day(day)
        broker.event("INFO", f"decision for day {datetime.fromtimestamp(day, timezone.utc):%Y-%m-%d}: target={target} action={action}")

    # 6) mark + report
    eq = broker.mark(bid)
    p = broker.position
    line = {"ts": ts, "equity": round(eq), "cash": round(broker.state["cash"]),
            "position": p["symbol"] if p else "IDR", "entry": round(p["entry"]) if p else "",
            "stop": round(p["stop"]) if p else "", "target_today": target or "IDR", "action": action,
            "halted": broker.state["halted"], "spread_btc_pct": books["BTCIDR"]["spread_pct"],
            "spread_eth_pct": books["ETHIDR"]["spread_pct"]}
    out = ROOT / "reports" / "paper_daily.csv"
    out.parent.mkdir(exist_ok=True)
    new = not out.exists()
    with open(out, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(line))
        if new:
            w.writeheader()
        w.writerow(line)
    print(f"{ts} [PAPER, UNVALIDATED] equity {eq:,.0f} | holding {line['position']} | target {line['target_today']} | {action}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
