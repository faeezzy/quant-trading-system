"""Trade client tests — no network, fake credentials. Signing + allowlist + permission guard."""
import pytest
import requests

from data.collectors.indodax_private import sign
from data.collectors.indodax_public import ExchangeError
from execution.live_order_client import ALLOWED, IndodaxTradeClient, PermissionUnsafe


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, ""

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def request(self, method, url, headers=None, timeout=None):
        self.calls.append((method, url, headers))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def mk(script):
    s = FakeSession(script)
    c = IndodaxTradeClient("KEYXXXX1234", "SECRET", session=s, sleep=lambda _: None, clock=lambda: 1_000.0)
    return c, s


def test_allowlist_has_no_withdraw_path():
    assert not any("withdraw" in p.lower() or "fiat" in p.lower() for _, p in ALLOWED)
    assert ("POST", "/api/v2/order") in ALLOWED and ("DELETE", "/api/v2/order") in ALLOWED


def test_blocks_unlisted_method_path():
    c, _ = mk([])
    with pytest.raises(PermissionError):
        c._request("POST", "/api/v2/capital/withdraw/apply")
    with pytest.raises(PermissionError):
        c._request("DELETE", "/api/v2/account")   # method not allowed for this path


def test_repr_hides_secret():
    c, _ = mk([])
    assert "SECRET" not in repr(c) and "KEYXXXX1234" not in repr(c)


def test_create_limit_is_signed_post_with_client_id():
    c, s = mk([FakeResp(200, {"status": "FILLED", "executedQty": "0.01", "cummulativeQuoteQty": "1000",
                              "orderId": "F1", "origQty": "0.01"})])
    c.create_order("BTCIDR", "BUY", "LIMIT", client_order_id="e-BTCIDR-5", quantity=0.01, price=100000)
    method, url, headers = s.calls[0]
    query = url.split("?", 1)[1]
    assert method == "POST" and "/api/v2/order?" in url
    assert "newClientOrderId=e-BTCIDR-5" in query and "side=BUY" in query and "type=LIMIT" in query
    assert "price=100000" in query and "quantity=0.01" in query and "timeInForce=GTC" in query
    assert headers["X-APIKEY"] == "KEYXXXX1234" and headers["Sign"] == sign("SECRET", query)


def test_market_buy_uses_quote_qty_not_quantity():
    c, s = mk([FakeResp(200, {"status": "FILLED", "executedQty": "0.0005", "cummulativeQuoteQty": "50000", "orderId": "F2"})])
    c.create_order("BTCIDR", "BUY", "MARKET", client_order_id="x-1", quote_qty=50000)
    query = s.calls[0][1].split("?", 1)[1]
    assert "quoteOrderQty=50000" in query and "quantity=" not in query and "type=MARKET" in query


def test_market_sell_uses_quantity_not_quote_qty():
    c, s = mk([FakeResp(200, {"status": "FILLED", "executedQty": "0.01", "cummulativeQuoteQty": "1000", "orderId": "F3"})])
    c.create_order("BTCIDR", "SELL", "MARKET", client_order_id="x-2", quantity=0.01)
    query = s.calls[0][1].split("?", 1)[1]
    assert "quantity=0.01" in query and "quoteOrderQty" not in query and "side=SELL" in query


def test_create_order_validates_args():
    c, _ = mk([])
    with pytest.raises(ValueError):
        c.create_order("BTCIDR", "BUY", "LIMIT", client_order_id="e", quantity=0.01)      # no price
    with pytest.raises(ValueError):
        c.create_order("BTCIDR", "BUY", "MARKET", client_order_id="e")                      # no quote_qty


def test_post_timeout_raises_not_retried():
    c, s = mk([requests.Timeout()])
    with pytest.raises(ExchangeError):
        c.create_order("BTCIDR", "SELL", "MARKET", client_order_id="x", quantity=0.01)
    assert len(s.calls) == 1   # a write is NOT retried blindly


def test_permission_guard():
    c, _ = mk([FakeResp(200, {"canTrade": True, "canWithdraw": False, "canWithdrawFiat": False, "balances": []})])
    assert c.assert_tradable_permissions()["canTrade"] is True

    c2, _ = mk([FakeResp(200, {"canTrade": True, "canWithdraw": True, "canWithdrawFiat": False})])
    with pytest.raises(PermissionUnsafe):
        c2.assert_tradable_permissions()

    c3, _ = mk([FakeResp(200, {"canTrade": False, "canWithdraw": False, "canWithdrawFiat": False})])
    with pytest.raises(PermissionUnsafe):
        c3.assert_tradable_permissions()
