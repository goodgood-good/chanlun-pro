"""app_monitor 双调 _market_settings 消费 runtime override event: register_recursive_monitor_jobs
先 _market_settings(判 enabled)后 from_config(内部二次 _market_settings), 而 _apply_runtime_overrides
的 record_runtime_override_application 首调即写 audit jsonl, 二调命中 _audit_log_has_event 去重返 None
→ web 部署 runtime override 应用通知(cfg.runtime_override_event, run_once:592 消费)永不发出;
CLI 单调故正常(新R3-F2-APPMON-3)。修复=from_config 接受预取 settings 复用, 单次 _market_settings。"""

from chanlun.recursive_bt.monitor import app_monitor


def test_from_config_reuses_prefetched_settings_preserves_override_event(monkeypatch):
    # 模拟 audit 去重: 第一次 _market_settings 返回带 event 的 settings, 第二次(event_key 已入
    # audit)返回不带 event 的 settings —— 复刻双调消费
    calls = {"n": 0}

    def fake_market_settings(market):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"enabled": True, "_runtime_override_event": {"event_key": "a|risk_reduce"}}
        return {"enabled": True}

    monkeypatch.setattr(app_monitor, "_market_settings", fake_market_settings)

    # 复刻 register: 先取 settings 判 enabled
    settings = app_monitor._market_settings("a")
    assert settings.get("enabled") is True
    # 修复后 from_config 复用该 settings(单次), 不再二次调 _market_settings 消费 event
    cfg = app_monitor.DynamicMonitorConfig.from_config("a", settings)
    assert cfg.runtime_override_event == {"event_key": "a|risk_reduce"}
    assert calls["n"] == 1  # 只调一次 _market_settings


def test_from_config_without_settings_still_fetches(monkeypatch):
    # 向后兼容: 不传 settings 时仍自行 _market_settings(现有 lookback 测试路径不破)
    monkeypatch.setattr(app_monitor, "_market_settings", lambda market: {"enabled": True})
    cfg = app_monitor.DynamicMonitorConfig.from_config("a")
    assert cfg.market == "a"