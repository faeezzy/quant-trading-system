"""Live trading runner — D2 weekly momentum rotation, REAL orders (status: UNVALIDATED LIVE).

DANGER: this places real orders. It stays inert until BOTH are true:
  - TRADING_MODE=live
  - LIVE_ARMED in (1,yes,true)      <- the arm/confirm guard for the first real orders
Without LIVE_ARMED it runs in OBSERVE mode: it reconciles, checks the stop and logs the decision it
WOULD make, but places nothing. Arm it only on the 24/7 host after the paper gate (spec 9) and owner
approval, and after verifying the POST field names against the docs (see live_order_client header).

Cadence (hybrid):
  default           full cycle (run hourly via cron): data -> reconcile -> stop -> daily decision -> mark/report
  --stop-watch      light run (run every ~10 min): lock -> fetch bid -> synthetic stop only

Each cycle reconciles with the exchange BEFORE acting, and the decided day is persisted so re-runs and
restarts never double-trade. Strategy params are the frozen D2 ones (lookback 28, SMA100, 3x ATR).
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.logging_setup import setup_logging  # noqa: E402
from data.collectors.historical import backfill, update  # noqa: E402
from data.collectors.indodax_public import ExchangeError, IndodaxPublicClient  # noqa: E402
from data.validators.market import validate_book  # noqa: E402
from database.candle_store import CandleStore  # noqa: E402
from execution.live_broker import LiveBroker, LiveConfig  # noqa: E402
from execution.live_order_client import IndodaxTradeClient, PermissionUnsafe  # noqa: E402
from scripts.paper_trade import decide  # noqa: E402  (pure decision fn; reused, not duplicated)
from scripts.scan_signals import load  # noqa: E402
from strategies import v3  # noqa: E402

SYMBOLS = ["BTCIDR", "ETHIDR"]
WARMUP_DAYS = 260


def is_armed() -> bool:
    return os.environ.get("LIVE_ARMED", "").strip().lower() in ("1", "yes", "true")


def utc_day(now: int) -> int:
    return now - (now % 86400)


def stop_check(broker: LiveBroker, bid: dict[str, float], armed: bool, day: int) -> str | None:
    """Synthetic stop vs live bid. Returns an action string if it fired, else None."""
    p = broker.position
    if not p or p["symbol"] not in bid:
        return None
    if bid[p["symbol"]] > p["stop"]:
        return None
    if not armed:
        broker.event("WARN", f"STOP would trigger for {p['symbol']} @ bid {bid[p['symbol']]:,.0f} (not armed)")
        return "STOP (observe only — not armed)"
    r = broker.flatten("stop", day)
    return f"STOP {r}" if r else "STOP exit FAILED — owner must flatten"


def execute_decision(broker: LiveBroker, target: str | None, bid: dict[str, float],
                     ask: dict[str, float], atrs: dict[str, float], day: int, armed: bool) -> str:
    """Act on the daily target (rotation). Observe-only unless armed. Pure w.r.t. network (uses broker)."""
    pos = broker.position
    action = ""
    if pos and target == pos["symbol"]:
        return "hold"
    if pos and target != pos["symbol"]:
        if not armed:
            action = f"WOULD EXIT {pos['symbol']}"
        else:
            r = broker.flatten("rotation_exit", day)
            action = (f"EXIT {r['symbol']} pnl {r['pnl']:,.0f} ({r['r']:+.2f}R)" if r else "EXIT FAILED")
    if broker.position is None and target:
        if not armed:
            action = (action + " | " if action else "") + f"WOULD ENTER {target}"
        else:
            p = broker.place_entry(target, ask[target], atrs[target], day, "rotation_entry")
            seg = (f"ENTER {target} qty {p['qty']:.8f} @ {p['entry']:,.0f} stop {p['stop']:,.0f}" if p
                   else f"enter {target} skipped (min order / not filled)")
            action = (action + " | " if action else "") + seg
    return action or ("stay in IDR" if not target else "none")


def _report(line: dict) -> None:
    out = ROOT / "reports" / "live_daily.csv"
    out.parent.mkdir(exist_ok=True)
    new = not out.exists()
    with open(out, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(line))
        if new:
            w.writeheader()
        w.writerow(line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live D2 runner (UNVALIDATED).")
    ap.add_argument("--stop-watch", action="store_true", help="light run: synthetic stop only")
    args = ap.parse_args(argv)

    load_dotenv(ROOT / ".env")
    if os.environ.get("TRADING_MODE") != "live":
        print("refusing: TRADING_MODE is not 'live'"); return 3
    lock = open(ROOT / "live_trade.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another live_trade instance is running; exiting"); return 0

    setup_logging("WARNING", str(ROOT / "logs" / "live_trade.log"))
    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
    risk = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text())
    armed = is_armed()
    pub = IndodaxPublicClient.from_config(cfg)
    trade = IndodaxTradeClient(base=cfg["endpoints"]["tapi_v2_public_base"],
                               timeout=cfg["http"]["timeout_seconds"], server_time_ms=lambda: pub.server_time()["server_time"])
    # Hard safety gate (spec 2.2/3.2): refuse to run on a key with withdrawal permission.
    try:
        trade.sync_clock()
        trade.assert_tradable_permissions()
    except (PermissionUnsafe, ExchangeError) as e:
        print(f"PERMISSION/EXCHANGE CHECK FAILED — not trading: {e}"); return 3

    live_cfg = LiveConfig(initial_equity=risk["initial_capital_idr"], max_allocation=risk["max_active_allocation_idr"],
                          risk_pct=risk["risk_per_trade_pct"], max_dd_pct_initial=risk["max_drawdown_pct_of_initial"])
    broker = LiveBroker(ROOT / "data" / "live.db", trade, live_cfg)
    now = int(time.time())
    day = utc_day(now)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # best bid/ask (needed for both modes)
    try:
        bid = {s: float(pub.book_ticker(s)["bidPrice"]) for s in SYMBOLS}
    except ExchangeError as e:
        print(f"{ts} NO ACTION — bid unavailable: {e}"); return 2

    if args.stop_watch:
        act = stop_check(broker, bid, armed, day)
        print(f"{ts} [STOP-WATCH{'' if armed else ', OBSERVE'}] {act or 'stop not hit'}")
        return 0

    # full cycle: reconcile with the exchange FIRST (spec 8)
    broker.reconcile()
    store = CandleStore(ROOT / "data" / "historical" / "candles.db")
    ask = {}
    try:
        for s in SYMBOLS:
            hi = store.bounds(s, "240")[1]
            if hi is None or now - hi > 3 * 86400:
                backfill(pub, store, s, "240", now - WARMUP_DAYS * 86400, now)
            else:
                update(pub, store, s, "240", now)
            b = pub.book_ticker(s)
            d = pub.depth(s, 20)
            chk = validate_book(b, d, cfg["data_quality"]["max_spread_pct"])
            if not chk.ok:
                raise ExchangeError(f"{s} book invalid: {chk.issues}")
            ask[s] = float(b["askPrice"])
    except ExchangeError as e:
        broker.event("ERROR", f"data unavailable, no action: {e}")
        print(f"{ts} NO ACTION — data unavailable: {e}"); return 2

    # synthetic stop before the daily decision
    stop_act = stop_check(broker, bid, armed, day)

    h4 = {s: load(store, s, "240") for s in SYMBOLS}
    h4 = {s: d[d["open_time"] >= now - WARMUP_DAYS * 86400].reset_index(drop=True) for s, d in h4.items()}
    frames = v3.weekly_rotation(h4)
    target, dday, atrs = decide(frames, broker.position["symbol"] if broker.position else None)

    action = "none"
    if broker.state["last_decision_day"] != dday:
        action = execute_decision(broker, target, bid, ask, atrs, dday, armed)
        broker.set_last_decision_day(dday)
        broker.event("INFO", f"decision day {datetime.fromtimestamp(dday, timezone.utc):%Y-%m-%d}: "
                             f"target={target} armed={armed} action={action}")

    eq = broker.mark(bid)
    p = broker.position
    _report({"ts": ts, "armed": armed, "equity": round(eq),
             "position": p["symbol"] if p else "IDR", "entry": round(p["entry"]) if p else "",
             "stop": round(p["stop"]) if p else "", "target_today": target or "IDR",
             "stop_action": stop_act or "", "action": action, "halted": broker.state["halted"]})
    print(f"{ts} [LIVE{'' if armed else ', OBSERVE'}] equity {eq:,.0f} | holding {p['symbol'] if p else 'IDR'} "
          f"| target {target or 'IDR'} | {action}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
