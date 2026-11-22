"""Watchdog — independent health checker that alerts the owner (spec 10).

Why separate from the trading runner: a bot that has crashed cannot report that it crashed. This
runs on its own cron (e.g. every 15 min) and alerts on: bot DOWN (heartbeat stale/missing), HALTED,
recovery back to ALIVE, and any new ERROR/CRITICAL event. Edge-triggered via a small state file so
it never spams the same condition every run.

Run:   python -m scripts.alert_check          # one check; sends alerts if state is bad
       python -m scripts.alert_check --test    # send a test alert and exit (verify Telegram setup)
Cron:  */15 * * * *  cd ~/quant-trading-system && .venv/bin/python -m scripts.alert_check >> logs/alert.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from core.alerts import Notifier  # noqa: E402
from scripts import status  # noqa: E402

STATE_PATH = ROOT / "reports" / "alert_state.json"


def decide_alerts(d: dict, prev: dict | None, now: datetime) -> tuple[list[tuple[str, str]], dict]:
    """Pure: given a status dict (from status.gather) and previous alert state, return
    (alerts, new_state). Edge-triggered: only transitions and unseen events alert.

    prev=None means first run -> establish a baseline (no replay of old events)."""
    hb, state = d["heartbeat"], d["state"]
    baseline = prev is None or prev.get("last_event_ts") is None
    prev = prev or {}

    down = state is None or bool(hb.get("missing")) or bool(hb.get("unparsable")) or (hb.get("fresh") is False)
    halted = bool(state and state.get("halted"))

    alerts: list[tuple[str, str]] = []
    if down and not prev.get("down"):
        age = hb.get("age_min")
        extra = f" (last run {age:.0f} min ago)" if isinstance(age, (int, float)) else ""
        alerts.append(("CRITICAL", f"bot DOWN — heartbeat stale/missing{extra}. Check the VM and logs/paper_cron.log"))
    if prev.get("down") and not down:
        alerts.append(("INFO", "bot back ALIVE — heartbeat fresh again"))
    if halted and not prev.get("halted"):
        alerts.append(("CRITICAL", "bot HALTED — new entries stopped; owner action needed"))
    if prev.get("halted") and not halted:
        alerts.append(("INFO", "halt cleared — bot running again"))

    # new ERROR/CRITICAL events (events are newest-first; walk oldest-first for order)
    last_ts = prev.get("last_event_ts") or ""
    new_last = last_ts
    for e in reversed(d["events"]):
        ts = e.get("ts", "")
        if ts > new_last:
            new_last = ts
        if not baseline and e.get("level") in ("ERROR", "CRITICAL") and ts > last_ts:
            alerts.append((e["level"], e.get("msg", "")))

    return alerts, {"down": down, "halted": halted, "last_event_ts": new_last or None}


def _load_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bot health watchdog + alerter.")
    ap.add_argument("--test", action="store_true", help="send a test alert and exit")
    args = ap.parse_args(argv)

    load_dotenv(ROOT / ".env")        # so TELEGRAM_* from .env reach the Notifier
    notifier = Notifier()
    if args.test:
        ok = notifier.send("INFO", "test alert — if you see this, alerts work ✅")
        print("sent" if ok else "not delivered (Telegram not configured? alert was logged only)")
        return 0 if ok else 1

    now = datetime.now(timezone.utc)
    d = status.gather(ROOT / "data" / "paper.db", ROOT / "reports" / "paper_daily.csv", now)
    prev = _load_state()
    alerts, new_state = decide_alerts(d, prev, now)
    for level, msg in alerts:
        notifier.send(level, msg)
    _save_state(new_state)

    if alerts:
        print(f"{now:%Y-%m-%d %H:%M UTC} sent {len(alerts)} alert(s): " + "; ".join(m for _, m in alerts))
    else:
        print(f"{now:%Y-%m-%d %H:%M UTC} ok — nothing to alert"
              + ("" if notifier.enabled else "  (note: Telegram not configured; log-only)"))
    return 0


if __name__ == "__main__":
    sys.exit(run())
