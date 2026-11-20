"""Paper broker — simulated spot execution with persistent state (spec 12.5). NEVER touches private APIs.

Purpose:  hold paper cash/positions in SQLite, simulate fills against LIVE best bid/ask with fees and
          slippage, enforce spec risk sizing, persist every action so a restart never loses or
          duplicates a position (spec 10.N, 19.9–19.12).
Inputs:   live book (bid/ask) supplied by caller; config dict (risk.yaml + paper section).
Outputs:  tables paper_state (single row, JSON), paper_trades, paper_events.
Fill model (conservative): BUY at ask*(1+slippage) + taker fee; SELL at bid*(1-slippage) - taker fee.
Stop:     synthetic, checked by the caller against live bid. It is a LOCAL stop, not an exchange order.
Failure:  any DB error propagates; caller must stop (no trading without reliable persistent state).
Tests:    tests/unit/test_paper_broker.py
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_state (id INTEGER PRIMARY KEY CHECK (id = 1), json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, ts TEXT, price REAL, qty REAL,
    notional REAL, fee REAL, reason TEXT, pnl REAL, r REAL);
CREATE TABLE IF NOT EXISTS paper_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, msg TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class PaperConfig:
    initial_equity: float = 1_000_000
    max_allocation: float = 500_000
    risk_pct: float = 0.25
    min_order_idr: float = 10_000
    taker_fee_pct: float = 0.2
    slippage_pct: float = 0.05
    stop_atr_mult: float = 3.0
    max_dd_pct_initial: float = 5.0


class PaperBroker:
    def __init__(self, db_path: str | Path, cfg: PaperConfig = PaperConfig()):
        self.cfg = cfg
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        row = self.conn.execute("SELECT json FROM paper_state WHERE id=1").fetchone()
        if row is None:
            self.state = {"cash": cfg.initial_equity, "position": None, "last_decision_day": None,
                          "peak_equity": cfg.initial_equity, "halted": False, "created": now_iso()}
            self._save()
        else:
            self.state = json.loads(row[0])

    # ---------- persistence ----------
    def _save(self) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO paper_state (id, json) VALUES (1, ?)", (json.dumps(self.state),))

    def event(self, level: str, msg: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO paper_events (ts, level, msg) VALUES (?,?,?)", (now_iso(), level, msg))

    # ---------- queries ----------
    @property
    def position(self) -> dict | None:
        return self.state["position"]

    def equity(self, bid: dict[str, float]) -> float:
        p = self.position
        return self.state["cash"] + (p["qty"] * bid[p["symbol"]] if p and p["symbol"] in bid else
                                     (p["qty"] * p["entry"] if p else 0.0))

    # ---------- actions ----------
    def buy(self, symbol: str, ask: float, atr: float, reason: str) -> dict | None:
        if self.position is not None:
            self.event("WARN", f"buy {symbol} refused: position already open ({self.position['symbol']})")
            return None
        if self.state["halted"]:
            self.event("WARN", f"buy {symbol} refused: halted (max drawdown) — owner must reset")
            return None
        c = self.cfg
        fill = ask * (1 + c.slippage_pct / 100)
        stop = fill - c.stop_atr_mult * atr
        if stop <= 0 or atr <= 0:
            self.event("WARN", f"buy {symbol} refused: invalid stop/ATR"); return None
        rt = 2 * c.taker_fee_pct / 100 + 2 * c.slippage_pct / 100
        per_unit = (fill - stop) + fill * rt
        eq = self.state["cash"]
        qty = (eq * c.risk_pct / 100) / per_unit
        notional = min(qty * fill, c.max_allocation, self.state["cash"] / (1 + c.taker_fee_pct / 100))
        if notional < c.min_order_idr:
            self.event("INFO", f"buy {symbol} skipped: notional {notional:,.0f} < min order {c.min_order_idr:,.0f}")
            return None
        qty = notional / fill
        fee = notional * c.taker_fee_pct / 100
        self.state["cash"] -= notional + fee
        self.state["position"] = {"symbol": symbol, "qty": qty, "entry": fill, "stop": stop, "init_stop": stop,
                                  "cost_basis": notional + fee, "risk_idr": qty * (fill - stop) + notional * rt,
                                  "opened": now_iso()}
        self._save()
        with self.conn:
            self.conn.execute("INSERT INTO paper_trades (symbol, side, ts, price, qty, notional, fee, reason) "
                              "VALUES (?,?,?,?,?,?,?,?)", (symbol, "BUY", now_iso(), fill, qty, notional, fee, reason))
        return self.position

    def sell(self, bid: float, reason: str) -> dict | None:
        p = self.position
        if p is None:
            return None
        c = self.cfg
        fill = bid * (1 - c.slippage_pct / 100)
        proceeds = p["qty"] * fill
        fee = proceeds * c.taker_fee_pct / 100
        pnl = proceeds - fee - p["cost_basis"]
        self.state["cash"] += proceeds - fee
        self.state["position"] = None
        self._save()
        with self.conn:
            self.conn.execute("INSERT INTO paper_trades (symbol, side, ts, price, qty, notional, fee, reason, pnl, r) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (p["symbol"], "SELL", now_iso(), fill, p["qty"], proceeds, fee, reason, pnl, pnl / p["risk_idr"]))
        return {"symbol": p["symbol"], "pnl": pnl, "r": pnl / p["risk_idr"], "reason": reason}

    def mark(self, bid: dict[str, float]) -> float:
        eq = self.equity(bid)
        self.state["peak_equity"] = max(self.state["peak_equity"], eq)
        if not self.state["halted"] and self.state["peak_equity"] - eq >= self.cfg.max_dd_pct_initial / 100 * self.cfg.initial_equity:
            self.state["halted"] = True
            self.event("CRITICAL", f"max drawdown breached (equity {eq:,.0f}); new entries halted until owner reset")
        self._save()
        return eq

    def set_last_decision_day(self, day_ts: int) -> None:
        self.state["last_decision_day"] = day_ts
        self._save()

    # ---------- halt control (kill switch / owner reset) ----------
    def halt(self, reason: str) -> bool:
        """Stop new entries (kill switch / owner). Idempotent; returns True if state changed.

        Does NOT close positions or cancel stops: the caller decides that. Stops keep
        being managed while halted (spec 3, 10)."""
        if self.state["halted"]:
            return False
        self.state["halted"] = True
        self._save()
        self.event("CRITICAL", f"HALTED by {reason}; new entries stopped until owner reset")
        return True

    def unhalt(self, reason: str) -> bool:
        """Owner re-enable after a halt (manual action, spec 3). Returns True if state changed."""
        if not self.state["halted"]:
            return False
        self.state["halted"] = False
        self._save()
        self.event("WARN", f"halt cleared by {reason}; new entries allowed")
        return True
