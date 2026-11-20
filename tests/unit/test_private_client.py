"""Private client tests — no network, fake credentials only."""
import pytest

from data.collectors.indodax_private import (
    READ_ONLY_PATHS, IndodaxPrivateReadClient, check_permissions, sign,
)
from data.collectors.indodax_public import ExchangeError
from scripts.check_account import fee_summary


def test_signature_matches_official_doc_example():
    # Values copied from INDODAX-TradeAPI-2-Private.md "SIGNED Endpoint Examples" (doc's dummy secret)
    secret = "f60617a68fcce028f0a90bc9eb765d17379eb548cc935c01a7ee3186eecf870e9b68f27a31bcfe8d"
    q = "symbol=btcidr&limit=100&timestamp=1578304294000&recvWindow=5000"
    assert sign(secret, q) == "58b503019a8f2754af7097b4d981913a8dca9e5cb2b408061cbd48736a940558"


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, ""

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return self.script.pop(0)


def mk(script, **kw):
    s = FakeSession(script)
    c = IndodaxPrivateReadClient("KEYXXXX1234", "SECRET", session=s, sleep=lambda _: None,
                                 clock=lambda: 1_000.0, **kw)
    return c, s


def test_missing_credentials_refuses(monkeypatch):
    monkeypatch.delenv("INDODAX_API_KEY", raising=False)
    monkeypatch.delenv("INDODAX_API_SECRET", raising=False)
    with pytest.raises(ValueError):
        IndodaxPrivateReadClient()


def test_request_is_signed_get_with_headers():
    c, s = mk([FakeResp(200, {"canTrade": False, "balances": []})])
    c.account()
    url, headers = s.calls[0]
    assert url.startswith("https://api.indodax.com/api/v2/account?")
    query = url.split("?", 1)[1]
    assert "timestamp=1000000" in query and "recvWindow=5000" in query
    assert headers["X-APIKEY"] == "KEYXXXX1234"
    assert headers["Sign"] == sign("SECRET", query)


def test_allowlist_blocks_anything_else():
    c, _ = mk([])
    with pytest.raises(PermissionError):
        c._get("/api/v2/capital/withdraw/apply")
    assert all(p.startswith("/api/v2/") for p in READ_ONLY_PATHS)
    assert not any("withdraw" in p or "fiat" in p for p in READ_ONLY_PATHS)


def test_client_has_no_write_methods():
    public = [m for m in dir(IndodaxPrivateReadClient) if not m.startswith("_")]
    for bad in ("create", "cancel", "withdraw", "post", "delete", "place", "buy", "sell"):
        assert not any(bad in m.lower() for m in public), bad
    assert not hasattr(FakeSession, "post")  # sanity: tests would fail if client tried POST


def test_repr_hides_secret():
    c, _ = mk([])
    assert "SECRET" not in repr(c) and "KEYXXXX1234" not in repr(c)


def test_timestamp_error_resyncs_once():
    c, s = mk([FakeResp(400, {"code": -1021, "msg": "Invalid timestamp"}), FakeResp(200, [])],
              server_time_ms=lambda: 1_002_500)
    assert c.open_orders() == []
    assert c._offset_ms == 2500
    assert "timestamp=1002500" in s.calls[1][0]


def test_auth_error_not_retried():
    c, s = mk([FakeResp(401, {"code": -1002, "msg": "Invalid credentials"})])
    with pytest.raises(ExchangeError) as e:
        c.account()
    assert e.value.code == -1002 and len(s.calls) == 1


def test_history_splits_into_7_day_windows():
    day = 86_400_000
    c, s = mk([FakeResp(200, {"data": [{"a": 1}]}), FakeResp(200, {"data": []}), FakeResp(200, {"data": [{"b": 2}]})])
    rows = c.my_trades("BTCIDR", 0, 20 * day)
    assert len(s.calls) == 3 and len(rows) == 2
    assert "symbol=btcidr" in s.calls[0][0]


def test_permission_guard():
    assert check_permissions({"canTrade": False, "canWithdraw": False, "canWithdrawFiat": False}, False) == []
    assert any("WITHDRAWAL" in v for v in check_permissions({"canTrade": False, "canWithdraw": True, "canWithdrawFiat": False}, False))
    assert any("missing" in v for v in check_permissions({"canTrade": False}, False))
    assert check_permissions({"canTrade": True, "canWithdraw": False, "canWithdrawFiat": False}, True) == []


def test_fee_summary_effective_rate():
    trades = [
        {"quoteQty": "100000", "commission": "300", "commissionAsset": "idr", "isMaker": False},
        {"quoteQty": "100000", "commission": "300", "commissionAsset": "idr", "isMaker": False},
        {"quoteQty": "50000", "commission": "60", "commissionAsset": "idr", "isMaker": True},
    ]
    f = fee_summary(trades)
    assert f["taker"]["fills"] == 2 and abs(f["taker"]["effective_pct"] - 0.3) < 1e-9
    assert abs(f["maker"]["effective_pct"] - 0.12) < 1e-9
