"""LogUtil 默认级别行为测试（P-001 perf 优化保护）。"""
import importlib
import logging

import pytest


def _fresh_logutil(monkeypatch, env: dict):
    """重置 LogUtil 单例并按 env 重新初始化，返回 (LogUtil, logger)。"""
    for k in ("LOG_LEVEL", "LOG_CONSOLE_LEVEL", "LOG_FILE_LEVEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import chanlun.tools.log_util as lu
    importlib.reload(lu)
    lu.LogUtil._logger = None
    # 清掉 logging 模块级残留 handler，确保级别重新生效
    _named = logging.getLogger("Logger")
    for h in list(_named.handlers):
        h.close()
    _named.handlers.clear()
    _named.setLevel(logging.NOTSET)   # 清除上轮测试残留 level，确保 get_logger 重新按 env 设级
    logger = lu.LogUtil.get_logger()
    return lu.LogUtil, logger


def test_root_logger_defaults_to_info(monkeypatch):
    """未设 LOG_LEVEL 时根 logger 默认 INFO，DEBUG 被 short-circuit。"""
    _LogUtil, logger = _fresh_logutil(monkeypatch, {})
    assert logger.level == logging.INFO
    assert logger.isEnabledFor(logging.INFO) is True
    assert logger.isEnabledFor(logging.DEBUG) is False


def test_log_level_env_override_to_debug(monkeypatch):
    """LOG_LEVEL=DEBUG 时根 logger 恢复 DEBUG（排查场景）。"""
    _LogUtil, logger = _fresh_logutil(monkeypatch, {"LOG_LEVEL": "DEBUG"})
    assert logger.level == logging.DEBUG
    assert logger.isEnabledFor(logging.DEBUG) is True


def test_warning_and_error_always_pass(monkeypatch):
    """默认 INFO 下 WARNING/ERROR 不受影响。"""
    _LogUtil, logger = _fresh_logutil(monkeypatch, {})
    assert logger.isEnabledFor(logging.WARNING) is True
    assert logger.isEnabledFor(logging.ERROR) is True
