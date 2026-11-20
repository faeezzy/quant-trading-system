"""Hand-built bar sequences with known outcomes."""
import numpy as np
import pandas as pd
import pytest

from backtesting.engine import BTConfig, metrics, run
from strategies.trend_breakout import Costs

ZERO = Costs(0, 0, 0, 0)


def bars(rows, atr=10.0):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["open_time"] = np.arange(len(df)) * 3600
    df["atr"] = atr
    df["signal"] = False
    df["h_ema_fast"] = 2.0
    df["h_ema_slow"] = 1.0
    return df


def cfg(**kw):
    base = dict(costs=ZERO, maker_fee_pct=0.0, regime_exit=False, daily_loss_pct=100, weekly_loss_pct=100)
    base.update(kw)
    return BTConfig(**base)


def test_taker_entry_next_open_and_stop_loss_is_minus_1r():
    df = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 100, 80, 85)])
    df.loc[0, "signal"] = True
    res = run({"X": df}, cfg())
    tr = res.trades.iloc[0]
    assert tr.entry == 100 and tr.reason == "stop"
    assert tr.exit == 85                       # stop 100-15=85; low 80 hits it
    assert tr.r == pytest.approx(-1.0)


def test_gap_through_stop_fills_at_open_worse_than_stop():
    df = bars([(100, 101, 99, 100), (100, 101, 99, 100), (70, 72, 60, 65)])
    df.loc[0, "signal"] = True
    tr = run({"X": df}, cfg()).trades.iloc[0]
    assert tr.exit == 70 and tr.r < -1.0


def test_maker_needs_trade_through_not_touch():
    touch = bars([(100, 101, 99, 100), (101, 103, 100, 102), (102, 104, 101, 103)])
    touch.loc[0, "signal"] = True
    assert run({"X": touch}, cfg(entry="maker")).trades.empty
    through = bars([(100, 101, 99, 100), (101, 103, 99.5, 102), (102, 104, 101, 103)])
    through.loc[0, "signal"] = True
    assert run({"X": through}, cfg(entry="maker")).trades.iloc[0].entry == 100


def test_same_bar_stop_and_target_assumes_stop():
    df = bars([(100, 101, 99, 100), (100, 200, 50, 100)])
    df.loc[0, "signal"] = True
    tr = run({"X": df}, cfg(exit_mode="target")).trades.iloc[0]
    assert tr.reason == "stop"


def test_target_exit():
    df = bars([(100, 101, 99, 100), (100, 101, 99, 100), (110, 150, 105, 140)])
    df.loc[0, "signal"] = True
    tr = run({"X": df}, cfg(exit_mode="target")).trades.iloc[0]
    assert tr.reason == "target" and tr.exit == 140 and tr.r == pytest.approx(40 / 15)


def test_trailing_stop_ratchets_up_never_down():
    rows = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 131, 100, 130), (130, 131, 120, 125), (125, 126, 105, 106)]
    df = bars(rows)
    df.loc[0, "signal"] = True
    tr = run({"X": df}, cfg(exit_mode="trail", trail_atr_mult=2.0)).trades.iloc[0]
    assert tr.reason == "stop" and tr.exit == 110      # trail set at 130-20 after bar 2, kept on bar 3


def test_risk_sizing_and_allocation_cap():
    df = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 100, 80, 85)])
    df.loc[0, "signal"] = True
    tr = run({"X": df}, cfg()).trades.iloc[0]
    assert tr.risk_idr == pytest.approx(2500)           # 0.25% of 1,000,000
    df2 = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 100, 99.9, 99.95)], atr=0.01)
    df2.loc[0, "signal"] = True
    tr2 = run({"X": df2}, cfg()).trades.iloc[0]
    assert tr2.notional <= 500_000 + 1e-6


def test_costs_reduce_pnl():
    df = bars([(100, 101, 99, 100), (100, 101, 99, 100), (110, 150, 105, 140)])
    df.loc[0, "signal"] = True
    free = run({"X": df}, cfg(exit_mode="target")).trades.iloc[0].pnl
    paid = run({"X": df}, cfg(exit_mode="target", costs=Costs(0.2, 0.2, 0.1, 0.05))).trades.iloc[0].pnl
    assert paid < free


def test_max_one_position_across_symbols():
    a = bars([(100, 101, 99, 100)] * 6); b = bars([(100, 101, 99, 100)] * 6)
    a.loc[0, "signal"] = True; b.loc[0, "signal"] = True; b.loc[1, "signal"] = True
    res = run({"A": a, "B": b}, cfg(max_hold_bars=100))
    assert len(res.trades) == 1 and res.trades.iloc[0].symbol == "A"


def test_metrics_basic():
    df = bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 100, 80, 85)])
    df.loc[0, "signal"] = True
    res = run({"X": df}, cfg())
    m = metrics(res, 1_000_000)
    assert m["trades"] == 1 and m["win_rate"] == 0 and m["net_pnl"] == pytest.approx(-2500, abs=1)
