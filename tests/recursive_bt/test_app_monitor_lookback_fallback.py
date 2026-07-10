"""Round11 B2 兄弟(app_monitor.from_config): DynamicMonitorConfig.from_config 在 config 未设
selection_lookback_bars 时回退到 3(盘后启动永远选不出候选), 与 dataclass 默认 48 / CLI
live_monitor:2308 的 48 分歧。fa9435b4(R11)只改了 dataclass 默认, 漏了此 from_config fallback;
committed 模板 config.py.demo 无 RECURSIVE_MONITOR_CONFIG → 新部署走空 config 触发。锁定 fallback>=48。"""

from chanlun.recursive_bt.monitor import app_monitor


def test_from_config_lookback_fallback_safe(monkeypatch):
    # 模拟 config 未设 selection_lookback_bars(新部署 config.py.demo 无 RECURSIVE_MONITOR_CONFIG)
    monkeypatch.setattr(app_monitor, "_market_settings", lambda market: {})
    cfg = app_monitor.DynamicMonitorConfig.from_config("a")
    # A 股选股池默认开(include_a_selection_pool)
    assert cfg.include_a_selection_pool is True
    # fallback 必须 >= 48(约一交易日 5m bar 数), 不得回退到致盘后漏选的 3
    # 与 dataclass 默认(line120) + CLI live_monitor:2308 一致
    assert cfg.a_selection_lookback_bars >= 48