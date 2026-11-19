from core.logging_setup import redact


def test_redacts_key_value_secrets():
    out = redact('{"api_key": "abc123secret", "sign=deadbeef&nonce=1"}')
    assert "abc123secret" not in out and "deadbeef" not in out


def test_redacts_env_secret(monkeypatch):
    monkeypatch.setenv("INDODAX_API_SECRET", "SUPERSECRETVALUE")
    assert "SUPERSECRETVALUE" not in redact("oops SUPERSECRETVALUE leaked")
