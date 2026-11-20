"""Phase 2: read-only account check.

Purpose:  verify the API key works, permissions are safe, and measure real fees from fills.
Inputs:   .env (INDODAX_API_KEY, INDODAX_API_SECRET), config/exchange.yaml.
Outputs:  stdout summary + reports/private/account_check_<UTC>.json (git-ignored).
Exit codes: 0 OK · 1 data issue · 2 exchange/auth error · 3 UNSAFE PERMISSIONS (stop, revoke key)
Run:      python -m scripts.check_account [--days 30]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.logging_setup import setup_logging  # noqa: E402
from data.collectors.indodax_private import IndodaxPrivateReadClient, check_permissions  # noqa: E402
from data.collectors.indodax_public import ExchangeError, IndodaxPublicClient  # noqa: E402

log = logging.getLogger("check_account")


def fee_summary(trades: list[dict]) -> dict:
    """Effective commission rate by maker/taker, from actual fills (fee + tax, per docs)."""
    agg: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"n": Decimal(0), "notional": Decimal(0), "commission": Decimal(0)})
    for t in trades:
        role = "maker" if t.get("isMaker") else "taker"
        if str(t.get("commissionAsset", "")).lower() != "idr":
            role += f"_{t.get('commissionAsset')}"
        a = agg[role]
        a["n"] += 1
        a["notional"] += Decimal(str(t.get("quoteQty", 0)))
        a["commission"] += Decimal(str(t.get("commission", 0)))
    return {r: {"fills": int(a["n"]), "notional": str(a["notional"]), "commission": str(a["commission"]),
                "effective_pct": float(a["commission"] / a["notional"] * 100) if a["notional"] else None}
            for r, a in agg.items()}


def run() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="history lookback (walked in 7-day chunks)")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    setup_logging("INFO", str(ROOT / "logs" / "check_account.log"))
    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
    pub = IndodaxPublicClient.from_config(cfg)

    try:
        client = IndodaxPrivateReadClient(
            base=cfg["endpoints"]["tapi_v2_public_base"],
            timeout=cfg["http"]["timeout_seconds"],
            server_time_ms=lambda: pub.server_time()["server_time"],
        )
    except ValueError as e:
        print(f"FAILED: {e}")
        return 2

    try:
        client.sync_clock()
        acct = client.account()
    except ExchangeError as e:
        print(f"FAILED: {e}")
        if e.code in (-1002, -2015, -2014):
            print("  -> key invalid/inactive/no permission. TAPIv2 needs a NEW TAPIv2 key (old TAPI keys don't work);"
                  " also confirm you clicked the activation email.")
        return 2

    perms = {k: acct.get(k) for k in ("canTrade", "canWithdraw", "canWithdrawFiat", "accountType")}
    print(f"permissions: {perms}")
    violations = check_permissions(acct, allow_trade=False)
    withdraw_bad = [v for v in violations if "WITHDRAW" in v or "withdrawal" in v]
    if withdraw_bad:
        print("\n!!! UNSAFE: " + "; ".join(withdraw_bad))
        print("!!! STOP. Delete this key at https://indodax.com/trade_api and create a new one WITHOUT withdrawal.")
        return 3
    for v in violations:
        print(f"  warning: {v}")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.days * 86400 * 1000
    report: dict = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "permissions": perms,
                    "balances": acct.get("balances", []), "markets": {}}
    rc = 0
    print("\nbalances (non-zero):")
    for b in acct.get("balances", []):
        print(f"  {b.get('asset'):>6}  free {b.get('free')}  locked {b.get('locked')}")
    if not acct.get("balances"):
        print("  (none)")

    try:
        oo = client.open_orders()
        report["open_orders"] = oo
        print(f"\nopen orders: {len(oo)}")
        for m in cfg["markets"]:
            sym = m["symbol"]
            orders = client.order_history(sym, start_ms, now_ms)
            trades = client.my_trades(sym, start_ms, now_ms)
            fees = fee_summary(trades)
            report["markets"][sym] = {"orders": orders, "trades": trades, "fees": fees}
            print(f"\n[{sym}] last {args.days}d: {len(orders)} orders, {len(trades)} fills")
            for role, f in fees.items():
                print(f"  {role}: {f['fills']} fills, notional IDR {f['notional']}, commission IDR {f['commission']}"
                      f" -> effective {f['effective_pct']:.4f}%" if f["effective_pct"] is not None else "")
    except ExchangeError as e:
        print(f"FAILED during history fetch: {e}")
        rc = 2

    out_dir = ROOT / "reports" / "private"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"account_check_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    out.chmod(0o600)
    print(f"\nreport (private, git-ignored): {out}")
    return rc


if __name__ == "__main__":
    sys.exit(run())
