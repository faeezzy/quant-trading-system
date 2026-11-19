"""HTTP client tests with a fake session (no network)."""
import pytest
import requests

from data.collectors.indodax_public import ExchangeError, IndodaxPublicClient


class FakeResp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script, self.calls, self.headers = list(script), [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(script):
    s = FakeSession(script)
    return IndodaxPublicClient(session=s, sleep=lambda _: None, min_interval=0, max_retries=2), s


def test_ok_and_correct_url():
    c, s = client([FakeResp(200, {"symbol": "BTCIDR", "bidPrice": "1", "askPrice": "2"})])
    assert c.book_ticker("BTCIDR")["symbol"] == "BTCIDR"
    assert s.calls[0] == ("https://api.indodax.com/api/v2/ticker/bookTicker", {"symbol": "BTCIDR"})


def test_retries_on_timeout_then_succeeds():
    c, s = client([requests.Timeout("t"), FakeResp(503), FakeResp(200, {"server_time": 1})])
    assert c.server_time() == {"server_time": 1}
    assert len(s.calls) == 3


def test_gives_up_after_max_retries():
    c, s = client([requests.ConnectionError("x")] * 3)
    with pytest.raises(ExchangeError):
        c.server_time()
    assert len(s.calls) == 3


def test_v2_error_envelope_not_retried():
    c, s = client([FakeResp(400, {"code": -1121, "msg": "Invalid symbol."})])
    with pytest.raises(ExchangeError) as e:
        c.depth("NOPE")
    assert e.value.code == -1121 and len(s.calls) == 1


def test_legacy_error_envelope():
    c, _ = client([FakeResp(200, {"error": "invalid_pair", "error_description": "Invalid Pair"})])
    with pytest.raises(ExchangeError):
        c.price_increments()


def test_non_json():
    c, _ = client([FakeResp(200, None, "<html>maintenance</html>")])
    with pytest.raises(ExchangeError, match="non-JSON"):
        c.pairs()


def test_client_is_get_only():
    public = [m for m in dir(IndodaxPublicClient) if not m.startswith("_")]
    forbidden = {"order", "trade", "cancel", "withdraw", "buy", "sell"}
    assert not any(f in m.lower() for m in public for f in forbidden)
