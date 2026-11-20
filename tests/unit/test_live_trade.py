"""Tests for the live runner's decision + guard logic (no network, FakeExchange-backed broker)."""
import pytest

from execution.fake_exchange import FakeExchange
from execution.live_broker import LiveBroker
from scripts.live_trade import execute_decision, is_armed, stop_check

ASK = 1_000_000_000
ATR = 30_000_000
DAY = 5 * 86400


def broker(tmp_path):
    ex = FakeExchange(bid={"BTCIDR": ASK, "ETHIDR": 50_000_000},
                      ask={"BTCIDR": ASK, "ETHIDR": 50_000_000}, balances={"IDR": 1_000_000})
    return LiveBroker(tmp_path / "live.db", ex), ex


BID = {"BTCIDR": ASK, "ETHIDR": 50_000_000}
ASKS = {"BTCIDR": ASK, "ETHIDR": 50_000_000}
ATRS = {"BTCIDR": ATR, "ETHIDR": 2_000_000}


def test_arm_gate(monkeypatch):
    monkeypatch.delenv("LIVE_ARMED", raising=False)
    assert is_armed() is False
    monkeypatch.setenv("LIVE_ARMED", "yes")
    assert is_armed() is True
    monkeypatch.setenv("LIVE_ARMED", "0")
    assert is_armed() is False


def test_observe_mode_places_nothing(tmp_path):
    b, _ = broker(tmp_path)
    action = execute_decision(b, "BTCIDR", BID, ASKS, ATRS, DAY, armed=False)
    assert "WOULD ENTER BTCIDR" in action and b.position is None


def test_armed_entry_opens_position(tmp_path):
    b, _ = broker(tmp_path)
    action = execute_decision(b, "BTCIDR", BID, ASKS, ATRS, DAY, armed=True)
    assert action.startswith("ENTER BTCIDR") and b.position["symbol"] == "BTCIDR"


def test_hold_when_target_equals_position(tmp_path):
    b, _ = broker(tmp_path)
    execute_decision(b, "BTCIDR", BID, ASKS, ATRS, DAY, armed=True)
    assert execute_decision(b, "BTCIDR", BID, ASKS, ATRS, DAY, armed=True) == "hold"


def test_rotation_exits_then_enters(tmp_path):
    b, _ = broker(tmp_path)
    execute_decision(b, "BTCIDR", BID, ASKS, ATRS, DAY, armed=True)
    action = execute_decision(b, "ETHIDR", BID, ASKS, ATRS, DAY, armed=True)
    assert "EXIT BTCIDR" in action and "ENTER ETHIDR" in action and b.position["symbol"] == "ETHIDR"


def test_stay_in_idr_when_no_target(tmp_path):
    b, _ = broker(tmp_path)
    assert execute_decision(b, None, BID, ASKS, ATRS, DAY, armed=True) == "stay in IDR"


def test_stop_check_observe_does_not_flatten(tmp_path):
    b, _ = broker(tmp_path)
    execute_decision(b, "BTCIDR", BID, ASKS, ATRS, DAY, armed=True)
    low = {"BTCIDR": b.position["stop"] - 1, "ETHIDR": 50_000_000}
    act = stop_check(b, low, armed=False, day=DAY)
    assert "not armed" in act and b.position is not None       # observed, not sold


def test_stop_check_armed_flattens(tmp_path):
    b, _ = broker(tmp_path)
    execute_decision(b, "BTCIDR", BID, ASKS, ATRS, DAY, armed=True)
    low = {"BTCIDR": b.position["stop"] - 1, "ETHIDR": 50_000_000}
    act = stop_check(b, low, armed=True, day=DAY)
    assert act.startswith("STOP") and b.position is None


def test_stop_check_not_triggered_above_stop(tmp_path):
    b, _ = broker(tmp_path)
    execute_decision(b, "BTCIDR", BID, ASKS, ATRS, DAY, armed=True)
    assert stop_check(b, BID, armed=True, day=DAY) is None and b.position is not None
