# -*- coding: utf-8 -*-
"""R11-#1: fundamental_roe_ann_min 显式 0(运维意图放宽 ROE 门槛)被 `or 8.0` 吞回默认 8%。

live_monitor.py:2328 与 app_monitor.py:219 均为 `float(cfg.get("fundamental_roe_ann_min") or 8.0)`,
Python `0 or 8.0 == 8.0` → 运维把阈值显式设 0(只要正增长、不设 ROE 下限)被无声吞成默认 8%,
与意图相反。同根 bug 已在 96ba3a04 修过一例(paper 卖出比例显式 0.0 被 or 1.0 吞成满额卖)。
修复=新增 _config_float(value, default)(仿 _config_int, `value is None or str(value).strip()==""` 判空,
保留显式 0),两处调用点改用它。本测试钉死 helper 语义 + 两调用点已切换(不再裸 or)。
"""
from pathlib import Path

from chanlun.recursive_bt.monitor.live_monitor import _config_float


def test_config_float_keeps_explicit_zero():
    # 核心: 显式 0 / 0.0 必须保留(bug 根源), 不被吞成默认
    assert _config_float(0, 8.0) == 0.0
    assert _config_float(0.0, 8.0) == 0.0
    assert _config_float("0", 8.0) == 0.0


def test_config_float_missing_falls_back():
    assert _config_float(None, 8.0) == 8.0
    assert _config_float("", 8.0) == 8.0
    assert _config_float("   ", 8.0) == 8.0


def test_config_float_bad_value_falls_back():
    assert _config_float("abc", 8.0) == 8.0
    assert _config_float([], 8.0) == 8.0


def test_config_float_valid_value():
    assert _config_float(5.5, 8.0) == 5.5
    assert _config_float("6", 8.0) == 6.0
    assert abs(_config_float("3.14", 0.0) - 3.14) < 1e-12


def test_call_sites_no_longer_bare_or():
    """两调用点必须改用 _config_float, 不再裸 `... or 8.0`(锁死 wiring)。"""
    root = Path(__file__).resolve().parents[2] / "src" / "chanlun" / "recursive_bt" / "monitor"
    lm = (root / "live_monitor.py").read_text(encoding="utf-8")
    am = (root / "app_monitor.py").read_text(encoding="utf-8")
    assert 'fundamental_roe_ann_min") or 8.0' not in lm
    assert 'fundamental_roe_ann_min") or 8.0' not in am
    assert "_config_float(" in lm
    assert "_config_float(" in am