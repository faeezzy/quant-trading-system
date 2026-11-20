"""Kill switch — stop the paper bot now (spec 10).

Purpose:  one command sets the bot to HALTED (no new entries) and, if asked, flattens the
          open position to IDR at the live bid. --reset re-enables (owner manual action, spec 3).
Scope:    paper mode (data/paper.db). Entries in paper fill immediately, so there are no resting
          entry orders to cancel; in live mode that step is added with the live broker.
Safety:   takes the same single-instance lock as paper_trade, so it never races a running cycle.
          A write action asks for confirmation unless --yes is given.
Manual fallback (strongest kill, spec 10): remove the cron line, and revoke the API key at
          https://indodax.com/trade_api.
Exit codes: 0 done (state changed or already in target state) · 1 bad usage/confirmation declined
            · 2 position close failed (network) — HALT still applied · 3 lock busy, try again
Run:      python -m scripts.kill_switch            # halt: stop new entries (reversible)
          python -m scripts.kill_switch --close    # halt + sell position to IDR at live bid
          python -m scripts.kill_switch --reset    # owner re-enable (clear halt)
          add --yes to skip the confirmation prompt
"""
from __future__ import annotations

import argparse
import fcntl
import signal
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from execution.paper_broker import PaperBroker, PaperConfig  # noqa: E402

LOCK_PATH = ROOT / "paper_trade.lock"
DB_PATH = ROOT / "data" / "paper.db"


class _LockTimeout(Exception):
    pass


def acquire_lock(path: Path, timeout_s: int = 20):
    """Block on the shared paper_trade lock, up to timeout_s. Raises _LockTimeout if busy."""
    fh = open(path, "w")

    def _alarm(_signum, _frame):
        raise _LockTimeout

    old = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout_s)
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    return fh


def _broker() -> PaperBroker:
    risk = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text())
    cfg = PaperConfig(
        initial_equity=risk["initial_capital_idr"], max_allocation=risk["max_active_allocation_idr"],
        risk_pct=risk["risk_per_trade_pct"], max_dd_pct_initial=risk["max_drawdown_pct_of_initial"])
    return PaperBroker(DB_PATH, cfg)


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Kill switch for the paper bot.")
    ap.add_argument("--close", action="store_true", help="also flatten the open position to IDR (live bid)")
    ap.add_argument("--reset", action="store_true", help="clear HALT and re-enable new entries (owner)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args(argv)

    if args.reset and (args.close):
        print("--reset cannot be combined with --close"); return 1
    if not DB_PATH.exists():
        print("no data/paper.db — bot has no state; nothing to do"); return 0

    try:
        lock = acquire_lock(LOCK_PATH)
    except _LockTimeout:
        print("a paper_trade cycle holds the lock; try again in a few seconds"); return 3

    try:
        broker = _broker()

        if args.reset:
            p = broker.position
            if p:
                print(f"note: position still open ({p['symbol']}); reset only clears HALT, it does not re-open trades")
            if not _confirm("Re-enable the bot (clear HALT)?", args.yes):
                print("aborted"); return 1
            changed = broker.unhalt("kill_switch --reset")
            print("HALT cleared — new entries allowed" if changed else "already running (not halted); nothing to do")
            return 0

        # default + --close: HALT
        action = "HALT the bot (stop new entries)" + (" and SELL the position to IDR" if args.close else "")
        if not _confirm(f"{action}?", args.yes):
            print("aborted"); return 1

        changed = broker.halt("kill_switch")
        print("HALTED — new entries stopped" if changed else "already halted")

        rc = 0
        if args.close:
            p = broker.position
            if not p:
                print("no open position to close")
            else:
                try:
                    from data.collectors.indodax_public import ExchangeError, IndodaxPublicClient
                    cfg = yaml.safe_load((ROOT / "config" / "exchange.yaml").read_text())
                    client = IndodaxPublicClient.from_config(cfg)
                    bid = float(client.book_ticker(p["symbol"])["bidPrice"])
                except (ExchangeError, Exception) as e:  # noqa: BLE001
                    print(f"!! could not fetch live bid to close {p['symbol']}: {e}")
                    print("!! HALT is applied but the position is STILL OPEN. Close manually or retry --close.")
                    broker.event("ERROR", f"kill_switch --close failed to get bid: {e}")
                    return 2
                res = broker.sell(bid, "kill_switch_close")
                broker.event("CRITICAL", f"kill_switch closed position: {res}")
                print(f"CLOSED {res['symbol']} at bid {bid:,.0f}  pnl {res['pnl']:+,.0f} IDR ({res['r']:+.2f}R)")

        print("\nStrongest kill (spec 10): remove the cron line and revoke the API key at https://indodax.com/trade_api")
        return rc
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    sys.exit(run())
