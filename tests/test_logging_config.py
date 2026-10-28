"""日志入口：configure_logging 要真正把 api.* 写进 backend.log。"""
from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

pytestmark = pytest.mark.no_db


def test_configure_logging_writes_api_logger(tmp_path, monkeypatch):
    import utils.logging_config as lc

    monkeypatch.setattr(lc, "LOG_DIR", tmp_path)
    lc.configure_logging()
    logging.getLogger("api.auth").info("session-history-ready")
    for handler in logging.getLogger().handlers:
        handler.flush()

    text = (tmp_path / "backend.log").read_text(encoding="utf-8")
    assert "api.auth" in text
    assert "session-history-ready" in text
    assert any(isinstance(h, TimedRotatingFileHandler) for h in logging.getLogger().handlers)
