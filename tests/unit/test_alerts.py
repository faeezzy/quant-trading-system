"""Tests for alerting (Notifier) and the watchdog decision logic. No network."""
from datetime import datetime, timezone

from core.alerts import Notifier
from scripts.alert_check import decide_alerts

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


# ---------- Notifier ----------

def test_notifier_disabled_logs_only():
    n = Notifier(token="", chat_id="")
    assert n.enabled is False
    assert n.send("INFO", "hi") is False          # no delivery, but no raise


def test_notifier_delivers_via_poster():
    seen = {}

    def poster(url, data):
        seen["url"], seen["data"] = url, data
        return True

    n = Notifier(token="T0KEN", chat_id="123", poster=poster)
    assert n.enabled is True
    assert n.send("CRITICAL", "halted") is True
    assert "bot" in seen["data"]["text"] and seen["data"]["chat_id"] == "123"
    assert "T0KEN" in seen["url"]


def test_notifier_never_raises_on_poster_error():
    def boom(url, data):
        raise RuntimeError("network down")

    n = Notifier(token="T", chat_id="1", poster=boom)
    assert n.send("CRITICAL", "x") is False        # swallowed


def test_notifier_repr_hides_token():
    n = Notifier(token="SECRET_TOKEN_1234", chat_id="1")
    assert "SECRET_TOKEN_1234" not in repr(n)


# ---------- decide_alerts ----------

def _status(fresh=True, halted=False, events=None, missing=False):
    hb = {"missing": missing, "fresh": fresh, "age_min": 10 if fresh else 200}
    if missing:
        hb = {"missing": True}
    return {"heartbeat": hb, "state": None if missing else {"halted": halted}, "events": events or []}


def test_baseline_does_not_replay_old_events():
    d = _status(events=[{"ts": "2026-11-21T07:00:00+00:00", "level": "CRITICAL", "msg": "old boom"}])
    alerts, state = decide_alerts(d, None, NOW)       # first run
    assert alerts == []                                # no replay
    assert state["last_event_ts"] == "2026-11-21T07:00:00+00:00"


def test_down_transition_alerts_once():
    prev = {"down": False, "halted": False, "last_event_ts": "2026-11-21T07:00:00+00:00"}
    down = _status(fresh=False)
    alerts, state = decide_alerts(down, prev, NOW)
    assert any(lvl == "CRITICAL" and "DOWN" in m for lvl, m in alerts)
    # second run while still down -> no repeat
    alerts2, _ = decide_alerts(down, state, NOW)
    assert alerts2 == []


def test_recovery_alerts():
    prev = {"down": True, "halted": False, "last_event_ts": "2026-11-21T07:00:00+00:00"}
    alerts, _ = decide_alerts(_status(fresh=True), prev, NOW)
    assert any("ALIVE" in m for _, m in alerts)


def test_halt_transition_alerts_once():
    prev = {"down": False, "halted": False, "last_event_ts": "2026-11-21T07:00:00+00:00"}
    halted = _status(halted=True)
    alerts, state = decide_alerts(halted, prev, NOW)
    assert any("HALTED" in m for _, m in alerts)
    alerts2, _ = decide_alerts(halted, state, NOW)
    assert alerts2 == []


def test_new_critical_event_alerts():
    prev = {"down": False, "halted": False, "last_event_ts": "2026-11-21T07:00:00+00:00"}
    d = _status(events=[{"ts": "2026-11-21T08:00:00+00:00", "level": "ERROR", "msg": "data unavailable"}])
    alerts, state = decide_alerts(d, prev, NOW)
    assert any("data unavailable" in m for _, m in alerts)
    # same event not re-alerted
    alerts2, _ = decide_alerts(d, state, NOW)
    assert alerts2 == []


def test_info_events_do_not_alert():
    prev = {"down": False, "halted": False, "last_event_ts": "2026-11-21T07:00:00+00:00"}
    d = _status(events=[{"ts": "2026-11-21T09:00:00+00:00", "level": "INFO", "msg": "stay in IDR"}])
    alerts, _ = decide_alerts(d, prev, NOW)
    assert alerts == []
