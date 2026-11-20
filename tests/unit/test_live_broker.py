"""LiveBroker tests against FakeExchange — entry, partial, timeout reconciliation, exit, restart."""
import pytest

from execution.fake_exchange import FakeExchange
from execution.live_broker import LiveBroker, LiveConfig

ASK = 1_000_000_000
ATR = 30_000_000


def fx(**kw):
    return FakeExchange(bid={"BTCIDR": ASK}, ask={"BTCIDR": ASK}, **kw)


def test_entry_fills_and_sizes_by_risk(tmp_path):
    b = LiveBroker(tmp_path / "live.db", fx())
    p = b.place_entry("BTCIDR", ASK, ATR, day=5, reason="rotation_entry")
    assert p is not None and p["symbol"] == "BTCIDR"
    loss_at_stop = p["qty"] * (p["entry"] - p["stop"])
    assert loss_at_stop < 2_600            # ~0.25% of 1,000,000, plus cost allowance
    assert b.get_order_row("e-BTCIDR-5")["status"] == "FILLED"


def test_entry_not_marketable_opens_nothing(tmp_path):
    ex = FakeExchange(bid={"BTCIDR": ASK}, ask={"BTCIDR": ASK})
    ex.set_touch("BTCIDR", bid=ASK, ask=ASK * 1.02)      # ask far above the entry cap
    b = LiveBroker(tmp_path / "live.db", ex)
    assert b.place_entry("BTCIDR", ASK, ATR, day=5, reason="t") is None
    assert b.position is None
    assert b.get_order_row("e-BTCIDR-5")["status"] == "CANCELED"


def test_entry_partial_keeps_filled_and_cancels_rest(tmp_path):
    ex = fx()
    ex.partial_ratio = 0.5
    b = LiveBroker(tmp_path / "live.db", ex)
    p = b.place_entry("BTCIDR", ASK, ATR, day=5, reason="t")
    assert p is not None
    full = b.place_entry  # noqa  (sanity: position is half of the intended qty)
    # intended qty recorded on the order row; filled is half of it
    row = b.get_order_row("e-BTCIDR-5")
    assert p["qty"] == pytest.approx(row["intended_qty"] * 0.5, rel=1e-6)


def test_entry_timeout_that_landed_is_recovered_by_reconcile(tmp_path):
    ex = fx()
    ex.fail_next_create = "landed"        # exchange fills, client sees a timeout
    b = LiveBroker(tmp_path / "live.db", ex)
    p = b.place_entry("BTCIDR", ASK, ATR, day=5, reason="t")
    assert p is not None and b.position is not None        # recovered, no double order
    assert b.get_order_row("e-BTCIDR-5")["status"] == "FILLED"


def test_entry_idempotent_when_already_filled(tmp_path):
    b = LiveBroker(tmp_path / "live.db", fx())
    b.place_entry("BTCIDR", ASK, ATR, day=5, reason="t")
    pos = dict(b.position)
    again = b.place_entry("BTCIDR", ASK, ATR, day=5, reason="t")   # same day -> refused (position open)
    assert again is None and b.position["opened"] == pos["opened"]


def test_flatten_market_closes_and_reports_r(tmp_path):
    b = LiveBroker(tmp_path / "live.db", fx())
    b.place_entry("BTCIDR", ASK, ATR, day=5, reason="t")
    res = b.flatten("rotation_exit", day=5)
    assert res is not None and b.position is None and isinstance(res["r"], float)


def test_flatten_timeout_that_landed_is_recovered(tmp_path):
    ex = fx()
    b = LiveBroker(tmp_path / "live.db", ex)
    b.place_entry("BTCIDR", ASK, ATR, day=5, reason="t")
    ex.fail_next_create = "landed"
    res = b.flatten("stop", day=5)
    assert res is not None and b.position is None


def test_reconcile_updates_new_to_filled(tmp_path):
    ex = fx()
    b = LiveBroker(tmp_path / "live.db", ex)
    ex.orders["z1"] = {"symbol": "btcidr", "orderId": "F9", "clientOrderId": "z1", "side": "BUY",
                       "type": "LIMIT", "status": "FILLED", "origQty": "0.01", "executedQty": "0.01",
                       "cummulativeQuoteQty": "10000000", "price": "1000000000"}
    b._record_order("z1", symbol="BTCIDR", side="BUY", type="LIMIT", status="NEW")
    notes = b.reconcile()
    assert b.get_order_row("z1")["status"] == "FILLED" and any("FILLED" in n for n in notes)


def test_halt_unhalt_and_restart(tmp_path):
    db = tmp_path / "live.db"
    b = LiveBroker(db, fx())
    b.place_entry("BTCIDR", ASK, ATR, day=5, reason="t")
    assert b.halt("kill") is True and b.halt("kill") is False
    b2 = LiveBroker(db, fx())                              # restart
    assert b2.state["halted"] and b2.position is not None  # state + position survive
    assert b2.unhalt("owner") is True


def test_drawdown_halts(tmp_path):
    b = LiveBroker(tmp_path / "live.db",
                   FakeExchange(bid={"BTCIDR": 100}, ask={"BTCIDR": 100}),
                   LiveConfig(risk_pct=50, max_allocation=1_000_000, stop_atr_mult=0.5, max_dd_pct_initial=1.0))
    b.place_entry("BTCIDR", 100, 10, day=5, reason="t")
    b.mark({"BTCIDR": 50})                                  # price halves
    assert b.state["halted"]
