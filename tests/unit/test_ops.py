"""Tests for the operator tools: status view (read-only) and kill switch. No network."""
from datetime import datetime, timedelta, timezone

import pytest

from execution.paper_broker import PaperBroker, PaperConfig
from scripts import kill_switch, status


# ---------- trade_stats ----------

def test_trade_stats_empty():
    s = status.trade_stats([])
    assert s["closed"] == 0 and s["profit_factor"] is None and s["expectancy_r"] is None


def test_trade_stats_profit_factor_and_expectancy():
    sells = [{"pnl": 300, "r": 1.5}, {"pnl": -100, "r": -0.5},
             {"pnl": 200, "r": 1.0}, {"pnl": -100, "r": -0.5}]
    s = status.trade_stats(sells)
    assert s["closed"] == 4 and s["wins"] == 2 and s["losses"] == 2
    assert s["profit_factor"] == pytest.approx(500 / 200)          # 2.5
    assert s["expectancy_r"] == pytest.approx((1.5 - 0.5 + 1.0 - 0.5) / 4)  # 0.375
    assert s["net_pnl"] == pytest.approx(300)


def test_trade_stats_no_losers_profit_factor_none():
    s = status.trade_stats([{"pnl": 100, "r": 1.0}])
    assert s["profit_factor"] is None and s["wins"] == 1 and s["losses"] == 0


# ---------- heartbeat ----------

def _write_csv(path, ts_str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"ts,equity,cash,position\n{ts_str},1003120,753120,BTCIDR\n")


def test_heartbeat_fresh(tmp_path):
    now = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
    _write_csv(tmp_path / "paper_daily.csv", "2026-11-20 14:20 UTC")
    hb = status.heartbeat(tmp_path / "paper_daily.csv", now)
    assert hb["fresh"] is True and hb["age_min"] == pytest.approx(10)


def test_heartbeat_stale(tmp_path):
    now = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
    _write_csv(tmp_path / "paper_daily.csv", "2026-11-20 12:00 UTC")
    hb = status.heartbeat(tmp_path / "paper_daily.csv", now)
    assert hb["fresh"] is False and hb["age_min"] > status.HEARTBEAT_MAX_MINUTES


def test_heartbeat_missing(tmp_path):
    hb = status.heartbeat(tmp_path / "nope.csv", datetime.now(timezone.utc))
    assert hb["missing"] is True


# ---------- gather + render exit codes ----------

def _seed_db(tmp_path, halted=False):
    b = PaperBroker(tmp_path / "paper.db", PaperConfig())
    b.buy("BTCIDR", ask=1_000_000_000, atr=30_000_000, reason="t")
    b.sell(1_100_000_000, "x")           # one closed, winning trade
    if halted:
        b.halt("test")
    b.conn.close()
    return tmp_path / "paper.db"


def test_gather_reads_closed_trade(tmp_path):
    db = _seed_db(tmp_path)
    now = datetime.now(timezone.utc)
    d = status.gather(db, tmp_path / "paper_daily.csv", now)
    assert d["stats"]["closed"] == 1 and d["state"]["position"] is None


def test_render_exit_code_no_state(tmp_path):
    now = datetime.now(timezone.utc)
    d = status.gather(tmp_path / "missing.db", tmp_path / "missing.csv", now)
    _, rc = status.render(d, now, 5)
    assert rc == 3


def test_render_exit_code_stale(tmp_path):
    db = _seed_db(tmp_path)
    now = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
    _write_csv(tmp_path / "paper_daily.csv", "2026-11-20 10:00 UTC")  # stale
    d = status.gather(db, tmp_path / "paper_daily.csv", now)
    _, rc = status.render(d, now, 5)
    assert rc == 1


def test_render_exit_code_halted(tmp_path):
    db = _seed_db(tmp_path, halted=True)
    now = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
    _write_csv(tmp_path / "paper_daily.csv", "2026-11-20 14:20 UTC")  # fresh
    d = status.gather(db, tmp_path / "paper_daily.csv", now)
    _, rc = status.render(d, now, 5)
    assert rc == 2   # halted dominates a fresh heartbeat


# ---------- broker halt/unhalt ----------

def test_broker_halt_unhalt_idempotent(tmp_path):
    b = PaperBroker(tmp_path / "paper.db", PaperConfig())
    assert b.halt("t") is True and b.state["halted"] is True
    assert b.halt("t") is False                    # already halted
    assert b.unhalt("t") is True and b.state["halted"] is False
    assert b.unhalt("t") is False


# ---------- kill switch CLI (no --close, so no network) ----------

def test_kill_switch_halts_and_resets(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)                          # flat, not halted
    monkeypatch.setattr(kill_switch, "DB_PATH", db)
    monkeypatch.setattr(kill_switch, "LOCK_PATH", tmp_path / "paper_trade.lock")

    assert kill_switch.run(["--yes"]) == 0
    assert PaperBroker(db, PaperConfig()).state["halted"] is True

    assert kill_switch.run(["--reset", "--yes"]) == 0
    assert PaperBroker(db, PaperConfig()).state["halted"] is False


def test_kill_switch_no_db(tmp_path, monkeypatch):
    monkeypatch.setattr(kill_switch, "DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(kill_switch, "LOCK_PATH", tmp_path / "paper_trade.lock")
    assert kill_switch.run(["--yes"]) == 0           # nothing to do, clean exit


def test_kill_switch_declined(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    monkeypatch.setattr(kill_switch, "DB_PATH", db)
    monkeypatch.setattr(kill_switch, "LOCK_PATH", tmp_path / "paper_trade.lock")
    # no --yes and input() returns "n"
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert kill_switch.run([]) == 1
    assert PaperBroker(db, PaperConfig()).state["halted"] is False  # unchanged
