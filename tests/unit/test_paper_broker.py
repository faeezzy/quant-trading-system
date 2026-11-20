import numpy as np
import pandas as pd
import pytest

from execution.paper_broker import PaperBroker, PaperConfig
from scripts.paper_trade import decide


def test_buy_sizes_by_risk_and_persists(tmp_path):
    b = PaperBroker(tmp_path / "p.db")
    p = b.buy("BTCIDR", ask=1_000_000_000, atr=30_000_000, reason="t")
    assert p is not None
    loss_at_stop = p["qty"] * (p["entry"] - p["stop"])
    assert loss_at_stop < 2_500 + 1e-6 and p["risk_idr"] == pytest.approx(2_500, rel=1e-6)
    b2 = PaperBroker(tmp_path / "p.db")                     # restart
    assert b2.position["symbol"] == "BTCIDR" and b2.state["cash"] == pytest.approx(b.state["cash"])


def test_no_second_position_and_sell_pnl(tmp_path):
    b = PaperBroker(tmp_path / "p.db")
    b.buy("BTCIDR", 100.0, 1.0, "t")
    assert b.buy("ETHIDR", 100.0, 1.0, "t") is None
    r = b.sell(110.0, "x")
    assert r["pnl"] > 0 and b.position is None
    assert b.sell(110.0, "x") is None


def test_min_order_skip(tmp_path):
    b = PaperBroker(tmp_path / "p.db", PaperConfig(min_order_idr=10_000_000))
    assert b.buy("BTCIDR", 100.0, 1.0, "t") is None and b.position is None


def test_drawdown_halts_new_entries(tmp_path):
    b = PaperBroker(tmp_path / "p.db", PaperConfig(risk_pct=50, max_allocation=1_000_000, stop_atr_mult=0.5))
    b.buy("BTCIDR", 100.0, 10.0, "t")
    b.mark({"BTCIDR": 10.0})
    assert b.state["halted"]
    b.sell(10.0, "x")
    assert b.buy("BTCIDR", 100.0, 10.0, "t") is None


def _frames(sig_a, exit_a, sig_b=False):
    def f(sig, ex):
        return pd.DataFrame({"open_time": [0, 86400], "signal": [False, sig], "exit_signal": [False, ex],
                             "atr": [1.0, 2.0]})
    return {"BTCIDR": f(sig_a, exit_a), "ETHIDR": f(sig_b, not sig_b)}


def test_decide_rules():
    t, day, atr = decide(_frames(True, False), None)
    assert t == "BTCIDR" and day == 86400 and atr["BTCIDR"] == 2.0
    t, _, _ = decide(_frames(False, True, sig_b=True), "BTCIDR")   # held BTC exits, rotate to ETH
    assert t == "ETHIDR"
    t, _, _ = decide(_frames(False, False), "BTCIDR")               # hold
    assert t == "BTCIDR"
    t, _, _ = decide(_frames(False, True), "BTCIDR")                # exit to cash
    assert t is None
