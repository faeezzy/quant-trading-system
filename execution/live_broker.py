"""Live broker — real spot execution with a persisted order lifecycle (spec 8, 9.3).

Status: UNVALIDATED LIVE. Tested only against FakeExchange. See docs/live_broker_design.md.

Principles:
  - The EXCHANGE is the source of truth. reconcile() rebuilds local state from the exchange
    (open orders + order lookup by client id) before any decision, and after any restart.
  - Placement is idempotent: every order carries a deterministic client order id, so a timeout is
    resolved by *querying* that id, never by blindly resending (spec 2.9).
  - Asymmetric policy: entry = marketable LIMIT with a slippage cap (miss = skip, costs nothing);
    exit/stop = MARKET (fill guaranteed; survival over price).
  - Partial fills: entry keeps the filled part and cancels the remainder; exit must reach flat.

Order-response field names (executedQty, cummulativeQuoteQty, status, orderId) are the Binance-shaped
fields the read client already implies; they are UNVERIFIED for INDODAX writes. Parsing is isolated in
_parse_order so one fix covers the real exchange. Tests: tests/unit/test_live_broker.py
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from data.collectors.indodax_public import ExchangeError

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_state (id INTEGER PRIMARY KEY CHECK (id = 1), json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_orders (
    client_order_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, type TEXT, reason TEXT,
    intended_price REAL, intended_qty REAL, quote_qty REAL,
    status TEXT, exchange_order_id TEXT, filled_qty REAL DEFAULT 0, avg_price REAL, fee REAL DEFAULT 0,
    created_ts TEXT, updated_ts TEXT);
CREATE TABLE IF NOT EXISTS live_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, msg TEXT);
"""

TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED", "LOST"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class LiveConfig:
    initial_equity: float = 1_000_000
    max_allocation: float = 250_000          # Tier 1 default (spec 3 ladder)
    risk_pct: float = 0.25
    min_order_idr: float = 10_000
    taker_fee_pct: float = 0.2
    entry_slippage_cap_pct: float = 0.3      # entry LIMIT is capped this far above the ask
    stop_atr_mult: float = 3.0
    max_dd_pct_initial: float = 5.0
    exit_max_retries: int = 3


def _parse_order(o: dict) -> dict:
    """Isolate the UNVERIFIED order-response shape. Returns normalized fields."""
    filled = float(o.get("executedQty") or 0)
    quote = float(o.get("cummulativeQuoteQty") or 0)
    return {"status": o.get("status", "NEW"), "filled_qty": filled,
            "avg_price": (quote / filled) if filled > 0 else None,
            "exchange_order_id": o.get("orderId"), "orig_qty": float(o.get("origQty") or 0)}


class LiveBroker:
    def __init__(self, db_path: str | Path, client, cfg: LiveConfig = LiveConfig()):
        self.cfg = cfg
        self.client = client
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        row = self.conn.execute("SELECT json FROM live_state WHERE id=1").fetchone()
        if row is None:
            self.state = {"position": None, "peak_equity": cfg.initial_equity, "halted": False,
                          "last_decision_day": None, "exit_seq": 0, "created": now_iso()}
            self._save()
        else:
            self.state = json.loads(row[0])

    # ---------- persistence ----------
    def _save(self) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO live_state (id, json) VALUES (1, ?)", (json.dumps(self.state),))

    def event(self, level: str, msg: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO live_events (ts, level, msg) VALUES (?,?,?)", (now_iso(), level, msg))

    def _record_order(self, cid: str, **f) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO live_orders (client_order_id, symbol, side, type, reason, "
                "intended_price, intended_qty, quote_qty, status, exchange_order_id, filled_qty, avg_price, "
                "fee, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, f.get("symbol"), f.get("side"), f.get("type"), f.get("reason"),
                 f.get("intended_price"), f.get("intended_qty"), f.get("quote_qty"), f.get("status"),
                 f.get("exchange_order_id"), f.get("filled_qty", 0), f.get("avg_price"), f.get("fee", 0),
                 f.get("created_ts", now_iso()), now_iso()))

    def _update_order(self, cid: str, **f) -> None:
        sets = ", ".join(f"{k}=?" for k in f)
        with self.conn:
            self.conn.execute(f"UPDATE live_orders SET {sets}, updated_ts=? WHERE client_order_id=?",
                              (*f.values(), now_iso(), cid))

    def get_order_row(self, cid: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM live_orders WHERE client_order_id=?", (cid,))
        r = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], r)) if r else None

    # ---------- queries ----------
    @property
    def position(self) -> dict | None:
        return self.state["position"]

    def equity(self, bid: dict[str, float]) -> float:
        p = self.position
        if not p:
            return self.state.get("cash_estimate", self.cfg.initial_equity)
        mark = bid.get(p["symbol"], p["entry"])
        return self.state.get("cash_estimate", self.cfg.initial_equity - p["cost_basis"]) + p["qty"] * mark

    # ---------- halt control ----------
    def halt(self, reason: str) -> bool:
        if self.state["halted"]:
            return False
        self.state["halted"] = True
        self._save()
        self.event("CRITICAL", f"HALTED by {reason}; new entries stopped until owner reset")
        return True

    def unhalt(self, reason: str) -> bool:
        if not self.state["halted"]:
            return False
        self.state["halted"] = False
        self._save()
        self.event("WARN", f"halt cleared by {reason}")
        return True

    # ---------- reconciliation (exchange is the source of truth) ----------
    def reconcile(self) -> list[str]:
        """Resolve every non-terminal local order against the exchange. Returns notes. Call each
        cycle and after any restart BEFORE deciding (spec 8)."""
        notes = []
        cur = self.conn.execute("SELECT client_order_id, symbol, status FROM live_orders "
                                "WHERE status NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED','LOST')")
        for cid, symbol, status in cur.fetchall():
            try:
                o = self.client.get_order(symbol, client_order_id=cid)
            except ExchangeError as e:
                if e.code == -2013:   # not found: it never landed
                    self._update_order(cid, status="LOST")
                    notes.append(f"{cid}: not on exchange -> LOST")
                else:
                    notes.append(f"{cid}: lookup failed ({e}); left {status}")
                continue
            p = _parse_order(o)
            self._update_order(cid, status=p["status"], filled_qty=p["filled_qty"],
                               avg_price=p["avg_price"], exchange_order_id=p["exchange_order_id"])
            notes.append(f"{cid}: {status} -> {p['status']} (filled {p['filled_qty']})")
        return notes

    # ---------- entry (asymmetric: capped marketable LIMIT) ----------
    def place_entry(self, symbol: str, ask: float, atr: float, day: int, reason: str) -> dict | None:
        if self.position is not None:
            self.event("WARN", f"entry {symbol} refused: position already open"); return None
        if self.state["halted"]:
            self.event("WARN", f"entry {symbol} refused: halted"); return None
        c = self.cfg
        cid = f"e-{symbol}-{day}"                       # stable for the day => restart-idempotent
        existing = self.get_order_row(cid)
        if existing and existing["status"] == "FILLED":
            self.event("INFO", f"entry {cid} already filled; skipping"); return self.position
        if atr <= 0:
            self.event("WARN", f"entry {symbol} refused: invalid ATR"); return None

        cap_price = ask * (1 + c.entry_slippage_cap_pct / 100)     # worst-case fill
        stop = cap_price - c.stop_atr_mult * atr
        if stop <= 0:
            self.event("WARN", f"entry {symbol} refused: invalid stop"); return None
        rt = 2 * c.taker_fee_pct / 100 + 2 * c.entry_slippage_cap_pct / 100
        per_unit = (cap_price - stop) + cap_price * rt
        qty = (c.initial_equity * c.risk_pct / 100) / per_unit
        notional = min(qty * cap_price, c.max_allocation)
        if notional < c.min_order_idr:
            self.event("INFO", f"entry {symbol} skipped: notional {notional:,.0f} < min {c.min_order_idr:,.0f}")
            return None
        qty = notional / cap_price

        self._record_order(cid, symbol=symbol, side="BUY", type="LIMIT", reason=reason,
                           intended_price=cap_price, intended_qty=qty, status="INTENDED")
        try:
            resp = self.client.create_order(symbol, "BUY", "LIMIT", client_order_id=cid,
                                            quantity=qty, price=cap_price)
            p = _parse_order(resp)
        except ExchangeError as e:
            # timeout: did it land? reconcile this id, then read the row.
            self.event("WARN", f"entry {cid} send error: {e}; reconciling")
            self._update_order(cid, status="UNKNOWN")
            self.reconcile()
            row = self.get_order_row(cid)
            p = {"status": row["status"], "filled_qty": row["filled_qty"] or 0,
                 "avg_price": row["avg_price"], "exchange_order_id": row["exchange_order_id"]}

        return self._settle_entry(cid, symbol, stop, qty, cap_price, p)

    def _settle_entry(self, cid, symbol, stop, want_qty, cap_price, p) -> dict | None:
        filled = p["filled_qty"]
        self._update_order(cid, status=p["status"], filled_qty=filled, avg_price=p["avg_price"],
                           exchange_order_id=p.get("exchange_order_id"))
        if filled <= 0:
            if p["status"] in ("NEW", "PARTIALLY_FILLED", "UNKNOWN"):
                # not filled (limit didn't cross or still open) — cancel so it can't fill later unmanaged
                self._safe_cancel(symbol, cid)
            self.event("INFO", f"entry {cid} not filled (status {p['status']})")
            return None
        entry = p["avg_price"] or cap_price
        if p["status"] == "PARTIALLY_FILLED" or filled < want_qty * 0.999:
            self._safe_cancel(symbol, cid)       # keep the filled part, drop the rest
            self.event("INFO", f"entry {cid} partial: filled {filled} of {want_qty}; remainder canceled")
        fee = filled * entry * self.cfg.taker_fee_pct / 100
        cost_basis = filled * entry + fee
        risk_idr = filled * (entry - stop) + filled * entry * (2 * self.cfg.taker_fee_pct / 100)
        self.state["position"] = {"symbol": symbol, "qty": filled, "entry": entry, "stop": stop,
                                  "init_stop": stop, "cost_basis": cost_basis, "risk_idr": risk_idr,
                                  "entry_client_id": cid, "opened": now_iso()}
        self.state["cash_estimate"] = self.state.get("cash_estimate", self.cfg.initial_equity) - cost_basis
        self._save()
        self.event("INFO", f"ENTER {symbol} qty {filled:.8f} @ {entry:,.0f} stop {stop:,.0f}")
        return self.position

    def _safe_cancel(self, symbol: str, cid: str) -> None:
        try:
            self.client.cancel_order(symbol, client_order_id=cid)
            self._update_order(cid, status="CANCELED")
        except ExchangeError as e:
            self.event("WARN", f"cancel {cid} failed: {e}")

    # ---------- exit / stop (MARKET, must reach flat) ----------
    def flatten(self, reason: str, day: int) -> dict | None:
        p = self.position
        if p is None:
            return None
        symbol = p["symbol"]
        for _ in range(self.cfg.exit_max_retries):
            self.state["exit_seq"] += 1
            self._save()
            cid = f"x-{symbol}-{day}-{self.state['exit_seq']}"
            self._record_order(cid, symbol=symbol, side="SELL", type="MARKET", reason=reason,
                               intended_qty=p["qty"], status="INTENDED")
            try:
                resp = self.client.create_order(symbol, "SELL", "MARKET", client_order_id=cid, quantity=p["qty"])
                pr = _parse_order(resp)
            except ExchangeError as e:
                self.event("WARN", f"exit {cid} send error: {e}; reconciling")
                self._update_order(cid, status="UNKNOWN")
                self.reconcile()
                row = self.get_order_row(cid)
                pr = {"status": row["status"], "filled_qty": row["filled_qty"] or 0, "avg_price": row["avg_price"]}
            self._update_order(cid, status=pr["status"], filled_qty=pr["filled_qty"], avg_price=pr["avg_price"])
            if pr["filled_qty"] and pr["filled_qty"] >= p["qty"] * 0.999:
                fill = pr["avg_price"]
                proceeds = pr["filled_qty"] * fill
                fee = proceeds * self.cfg.taker_fee_pct / 100
                pnl = proceeds - fee - p["cost_basis"]
                self.state["cash_estimate"] = self.state.get("cash_estimate", 0.0) + proceeds - fee
                self.state["position"] = None
                self._save()
                r = pnl / p["risk_idr"] if p["risk_idr"] else 0.0
                self.event("INFO", f"EXIT {symbol} ({reason}) pnl {pnl:,.0f} ({r:+.2f}R)")
                return {"symbol": symbol, "pnl": pnl, "r": r, "reason": reason}
            self.event("WARN", f"exit {cid} not flat (filled {pr['filled_qty']} of {p['qty']}); retrying")
        self.event("CRITICAL", f"EXIT FAILED for {symbol} after {self.cfg.exit_max_retries} tries — "
                               "position STILL OPEN; owner must flatten manually")
        self.halt("exit_failed")
        return None

    # ---------- marking ----------
    def mark(self, bid: dict[str, float]) -> float:
        eq = self.equity(bid)
        self.state["peak_equity"] = max(self.state["peak_equity"], eq)
        if not self.state["halted"] and self.state["peak_equity"] - eq >= self.cfg.max_dd_pct_initial / 100 * self.cfg.initial_equity:
            self.halt("max_drawdown")
        self._save()
        return eq

    def set_last_decision_day(self, day: int) -> None:
        self.state["last_decision_day"] = day
        self._save()
