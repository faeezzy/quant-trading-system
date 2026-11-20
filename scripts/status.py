"""Operator status — is the paper bot alive, what does it hold, is it making money? (spec 10).

Purpose:  a 10-second health view. READ-ONLY: it opens paper.db and paper_daily.csv and
          prints; it never trades, never mutates state, and (by default) never hits the network.
Inputs:   data/paper.db (authoritative state), reports/paper_daily.csv (heartbeat + last mark).
Outputs:  stdout summary. With --json, a machine-readable dict instead.
Options:  --live  recompute equity from a fresh public best-bid (network); degrades gracefully.
          --events N  show the last N events (default 5).
Exit codes: 0 healthy (fresh heartbeat, not halted) · 1 STALE (heartbeat > 70 min — bot likely down)
            · 2 HALTED (entries stopped; owner attention) · 3 no state yet / db missing
Run:      python -m scripts.status [--live] [--events 10] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HEARTBEAT_MAX_MINUTES = 70  # spec 10: last paper_daily.csv line older than this => bot is down
CSV_TS_FMT = "%Y-%m-%d %H:%M UTC"


def trade_stats(sells: list[dict]) -> dict:
    """Performance from closed (SELL) trades. Pure; expectancy and profit factor, not win rate (spec 1)."""
    pnls = [float(s["pnl"]) for s in sells if s["pnl"] is not None]
    rs = [float(s["r"]) for s in sells if s["r"] is not None]
    n = len(pnls)
    if n == 0:
        return {"closed": 0, "net_pnl": 0.0, "profit_factor": None, "expectancy_r": None,
                "wins": 0, "losses": 0, "avg_win_r": None, "avg_loss_r": None}
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    win_rs = [r for r in rs if r > 0]
    loss_rs = [r for r in rs if r <= 0]
    return {
        "closed": n,
        "net_pnl": sum(pnls),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,  # None = no losers yet
        "expectancy_r": (sum(rs) / len(rs)) if rs else None,
        "wins": len(win_rs),
        "losses": len(loss_rs),
        "avg_win_r": (sum(win_rs) / len(win_rs)) if win_rs else None,
        "avg_loss_r": (sum(loss_rs) / len(loss_rs)) if loss_rs else None,
    }


def heartbeat(csv_path: Path, now: datetime) -> dict:
    """Age of the last report line. Returns {fresh, age_min, last_ts, last_row} or {missing:True}."""
    if not csv_path.exists():
        return {"missing": True}
    rows = csv_path.read_text().splitlines()
    if len(rows) < 2:
        return {"missing": True}
    header = rows[0].split(",")
    last = dict(zip(header, rows[-1].split(",")))
    try:
        last_ts = datetime.strptime(last["ts"], CSV_TS_FMT).replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return {"missing": False, "unparsable": True, "last_row": last}
    age_min = (now - last_ts).total_seconds() / 60
    return {"missing": False, "fresh": age_min <= HEARTBEAT_MAX_MINUTES,
            "age_min": age_min, "last_ts": last_ts, "last_row": last}


def gather(db_path: Path, csv_path: Path, now: datetime, live_bid: dict | None = None) -> dict:
    """Collect everything the status view needs. Read-only."""
    hb = heartbeat(csv_path, now)
    if not db_path.exists():
        return {"state": None, "heartbeat": hb, "stats": trade_stats([]), "events": []}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT json FROM paper_state WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else None
        sells = [dict(zip(("ts", "symbol", "pnl", "r"), r)) for r in conn.execute(
            "SELECT ts, symbol, pnl, r FROM paper_trades WHERE side='SELL' ORDER BY id").fetchall()]
        events = [dict(zip(("ts", "level", "msg"), r)) for r in conn.execute(
            "SELECT ts, level, msg FROM paper_events ORDER BY id DESC LIMIT 50").fetchall()]
    finally:
        conn.close()

    equity = None
    equity_asof = "last run"
    if live_bid is not None and state and state.get("position"):
        p = state["position"]
        if p["symbol"] in live_bid:
            equity = state["cash"] + p["qty"] * live_bid[p["symbol"]]
            equity_asof = "live"
    elif live_bid is not None and state and not state.get("position"):
        equity = state["cash"]
        equity_asof = "live"
    if equity is None and hb.get("last_row", {}).get("equity"):
        try:
            equity = float(hb["last_row"]["equity"])
        except ValueError:
            pass
    return {"state": state, "heartbeat": hb, "stats": trade_stats(sells),
            "events": events, "equity": equity, "equity_asof": equity_asof}


def _fmt_idr(x) -> str:
    return f"IDR {x:,.0f}" if x is not None else "—"


def render(d: dict, now: datetime, n_events: int) -> tuple[str, int]:
    """Human-readable report + exit code."""
    state, hb, st = d["state"], d["heartbeat"], d["stats"]
    L = [f"INDODAX Quant — Paper Bot Status        {now:%Y-%m-%d %H:%M UTC}",
         "─" * 62]

    # heartbeat / exit code
    rc = 0
    if state is None:
        L.append("STATE       (none) — bot has not run yet; no data/paper.db")
        return "\n".join(L), 3
    if hb.get("missing"):
        L.append("HEARTBEAT   ✗ no report yet (reports/paper_daily.csv missing/empty)")
        rc = 1
    elif hb.get("unparsable"):
        L.append("HEARTBEAT   ? last report line unparsable")
        rc = 1
    else:
        age = hb["age_min"]
        mark = "● ALIVE" if hb["fresh"] else "✗ STALE"
        if not hb["fresh"]:
            rc = 1
        L.append(f"HEARTBEAT   {mark}   last run {age:.0f} min ago ({hb['last_ts']:%Y-%m-%d %H:%M UTC})")
        if not hb["fresh"]:
            L.append(f"            > {HEARTBEAT_MAX_MINUTES} min old — bot likely down; check logs/paper_cron.log")

    halted = bool(state.get("halted"))
    if halted:
        rc = max(rc, 2)
    L.append(f"MODE        paper              HALTED: {'YES — entries stopped' if halted else 'no'}")

    # equity / drawdown
    init = state.get("peak_equity")  # fallback; real initial from config below if available
    eq = d.get("equity")
    L.append(f"EQUITY      {_fmt_idr(eq)}   (as of {d.get('equity_asof','last run')})")
    L.append(f"CASH        {_fmt_idr(state.get('cash'))}")

    p = state.get("position")
    if p:
        L.append(f"POSITION    {p['symbol']}  qty {p['qty']:.8f}  entry {p['entry']:,.0f}  stop {p['stop']:,.0f}")
        if d.get("equity_asof") == "live" and eq is not None:
            unreal = eq - state["cash"] - p["cost_basis"]
            L.append(f"            unrealized {unreal:+,.0f} IDR (live)")
        else:
            L.append("            unrealized — (run with --live for mark-to-market)")
    else:
        L.append("POSITION    IDR (flat)")

    peak = state.get("peak_equity")
    if peak and eq is not None:
        dd = (peak - eq) / peak * 100
        L.append(f"DRAWDOWN    peak {peak:,.0f} → now {eq:,.0f}   ({-dd:.2f}% from peak)")

    # performance
    L.append("")
    L.append(f"PERFORMANCE ({st['closed']} closed trade{'s' if st['closed'] != 1 else ''})")
    if st["closed"] == 0:
        L.append("            no closed trades yet (D2 is low-turnover; flat weeks are normal, spec 5)")
    else:
        pf = f"{st['profit_factor']:.2f}" if st["profit_factor"] is not None else "∞ (no losers)"
        exp = f"{st['expectancy_r']:+.2f}R" if st["expectancy_r"] is not None else "—"
        L.append(f"            net P&L     {st['net_pnl']:+,.0f} IDR")
        L.append(f"            profit factor {pf}      expectancy {exp}")
        aw = f"{st['avg_win_r']:+.2f}R" if st["avg_win_r"] is not None else "—"
        al = f"{st['avg_loss_r']:+.2f}R" if st["avg_loss_r"] is not None else "—"
        L.append(f"            wins {st['wins']} / losses {st['losses']}  (avg win {aw}, avg loss {al})")

    # events
    evs = d["events"][:n_events]
    if evs:
        L.append("")
        L.append(f"RECENT EVENTS (last {len(evs)})")
        for e in evs:
            L.append(f"  {e['ts']}  {e['level']:<8} {e['msg']}")

    return "\n".join(L), rc


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Paper bot status (read-only).")
    ap.add_argument("--live", action="store_true", help="fetch a fresh best-bid to mark equity (network)")
    ap.add_argument("--events", type=int, default=5, help="number of recent events to show")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    db_path = ROOT / "data" / "paper.db"
    csv_path = ROOT / "reports" / "paper_daily.csv"
    now = datetime.now(timezone.utc)

    live_bid = None
    if args.live:
        try:
            import time

            from data.collectors.indodax_public import IndodaxPublicClient
            cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
            client = IndodaxPublicClient.from_config(cfg)
            live_bid = {}
            for s in ("BTCIDR", "ETHIDR"):
                try:
                    live_bid[s] = float(client.book_ticker(s)["bidPrice"])
                except Exception:  # one symbol failing must not break status
                    pass
            _ = time
        except Exception as e:
            print(f"(--live unavailable, falling back to last-run mark: {e})", file=sys.stderr)
            live_bid = None

    d = gather(db_path, csv_path, now, live_bid)

    if args.json:
        out = {k: v for k, v in d.items() if k != "heartbeat"}
        hb = dict(d["heartbeat"])
        if hb.get("last_ts"):
            hb["last_ts"] = hb["last_ts"].isoformat()
        out["heartbeat"] = hb
        _, rc = render(d, now, args.events)
        out["exit_code"] = rc
        print(json.dumps(out, indent=2, default=str))
        return rc

    text, rc = render(d, now, args.events)
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(run())
