"""Alerting — push a short message to the owner (Telegram), and always log it too.

Design rules:
  - NEVER raises. An alert failure must not break or block the trading bot.
  - Telegram is used only if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set; otherwise log-only.
  - The bot token is a secret: it is never logged and never put in an event/report.
  - Messages are short and carry no credentials.

Setup (owner, ~2 min): message @BotFather on Telegram -> /newbot -> copy the token; then message
your new bot once and open https://api.telegram.org/bot<TOKEN>/getUpdates to read your chat id.
Put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.

Tests: tests/unit/test_alerts.py
"""
from __future__ import annotations

import logging
import os
from typing import Callable

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 poster: Callable[[str, dict], bool] | None = None, timeout: float = 10.0):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout
        self._poster = poster     # injectable for tests: (url, data) -> ok

    def __repr__(self) -> str:    # never leak the token
        tail = self.token[-4:] if self.token else ""
        return f"Notifier(enabled={self.enabled}, chat={self.chat_id or '-'}, token=***{tail})"

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, level: str, msg: str) -> bool:
        """Log always; deliver to Telegram if configured. Returns True if delivered. Never raises."""
        text = f"[{level}] INDODAX bot — {msg}"
        log.warning("ALERT %s", text)                 # the log never contains the token
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": text}
        try:
            if self._poster is not None:
                return bool(self._poster(url, data))
            import requests
            r = requests.post(url, data=data, timeout=self.timeout)
            return r.status_code == 200
        except Exception as e:                          # noqa: BLE001 — alerting must never raise
            log.warning("alert delivery failed: %s", type(e).__name__)
            return False
