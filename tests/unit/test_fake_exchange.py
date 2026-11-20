"""Tests for the FakeExchange test double itself (so broker tests rest on trusted behaviour)."""
import pytest

from data.collectors.indodax_public import ExchangeError
from execution.fake_exchange import FakeExchange


def mk():
    return FakeExchange(bid={"BTCIDR": 1_000_000_000}, ask={"BTCIDR": 1_000_000_000})


def test_market_buy_fills_and_updates_balances():
    fx = mk()
    o = fx.create_order("BTCIDR", "BUY", "MARKET", client_order_id="c1", quote_qty=50_000_000)
    assert o["status"] == "FILLED" and float(o["executedQty"]) == pytest.approx(0.05)
    assert fx.bal["BTC"] == pytest.approx(0.05) and len(fx.my_trades("BTCIDR", 0, 10)) == 1


def test_market_sell_reduces_coin():
    fx = FakeExchange(bid={"BTCIDR": 1_000_000_000}, ask={"BTCIDR": 1_000_000_000}, balances={"BTC": 0.1})
    fx.create_order("BTCIDR", "SELL", "MARKET", client_order_id="c2", quantity=0.1)
    assert fx.bal["BTC"] == pytest.approx(0.0)


def test_limit_not_marketable_stays_open():
    fx = mk()
    o = fx.create_order("BTCIDR", "BUY", "LIMIT", client_order_id="c3", quantity=0.01, price=900_000_000)
    assert o["status"] == "NEW" and float(o["executedQty"]) == 0
    assert fx.open_orders("BTCIDR")[0]["clientOrderId"] == "c3"


def test_partial_fill():
    fx = mk()
    fx.partial_ratio = 0.4
    o = fx.create_order("BTCIDR", "BUY", "LIMIT", client_order_id="c4", quantity=0.01, price=1_000_000_000)
    assert o["status"] == "PARTIALLY_FILLED" and float(o["executedQty"]) == pytest.approx(0.004)


def test_timeout_lost_places_nothing():
    fx = mk()
    fx.fail_next_create = "lost"
    with pytest.raises(ExchangeError):
        fx.create_order("BTCIDR", "BUY", "MARKET", client_order_id="c5", quote_qty=10_000_000)
    with pytest.raises(ExchangeError):
        fx.get_order("BTCIDR", client_order_id="c5")       # never recorded


def test_timeout_landed_records_fill_then_raises():
    fx = mk()
    fx.fail_next_create = "landed"
    with pytest.raises(ExchangeError):
        fx.create_order("BTCIDR", "BUY", "MARKET", client_order_id="c6", quote_qty=10_000_000)
    o = fx.get_order("BTCIDR", client_order_id="c6")        # but it DID land
    assert o["status"] == "FILLED" and float(o["executedQty"]) > 0


def test_reject():
    fx = mk()
    fx.reject_next = True
    with pytest.raises(ExchangeError):
        fx.create_order("BTCIDR", "BUY", "MARKET", client_order_id="c7", quote_qty=10_000_000)
    assert fx.get_order("BTCIDR", client_order_id="c7")["status"] == "REJECTED"
