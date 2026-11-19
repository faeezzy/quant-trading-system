"""Structured JSON logging with credential redaction.

Purpose:  one logger format for every module; secrets never reach log files.
Inputs:   log level, optional log file path.
Outputs:  configured root logger emitting one JSON object per line.
Failure:  if the log directory can't be created, falls back to stderr only.
Tests:    tests/unit/test_logging.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|api[_-]?secret|secret|sign|signature|token|authorization)"
               r"(\"?\s*[:=]\s*\"?)([^\s\",&]+)"),
]


def redact(text: str) -> str:
    """Mask anything that looks like key=value credentials, plus live env secrets."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***", text)
    for env in ("INDODAX_API_KEY", "INDODAX_API_SECRET"):
        val = os.environ.get(env)
        if val and len(val) >= 6:
            text = text.replace(val, "***REDACTED***")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    fmt = JsonFormatter()
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:  # never silent
            root.warning("file logging disabled", extra={"ctx": {"error": str(e)}})
