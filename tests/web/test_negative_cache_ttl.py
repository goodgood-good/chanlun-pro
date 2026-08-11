"""锁定负缓存分级 TTL（C1+M3 协同修复 阶段 A）。

M3：原负缓存单一 300s TTL 不区分「真空(新股/退市,真没数据)」与「异常空(数据源暂时不可用)」。
C1 让 cq 缺段返回 attrs['fetch_incomplete'] 的空 DataFrame,若仍走 300s 负缓存 → 数据源暂时
失败时 SSE/轮询 5 分钟不自愈(比带洞更糟)。本阶段给 _mark_negative_cache 增可选 ttl:
正常空结果保持 300s，异常空传使用 30s 短退避以便快速自愈。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

import cl_app.services.chart_cache as cc  # noqa: E402


def test_default_ttl_is_300_seconds(monkeypatch):
    # 不传 ttl → 保持 300s(所有现有调用点行为不变)。
    t = [10000.0]
    monkeypatch.setattr(cc.time, "time", lambda: t[0])
    cc._mark_negative_cache("k_default")
    t[0] = 10000 + 299
    assert cc._is_negatively_cached("k_default") is True   # 299s < 300
    t[0] = 10000 + 301
    assert cc._is_negatively_cached("k_default") is False  # 301s > 300


def test_short_ttl_expires_at_30s(monkeypatch):
    # 异常空短退避:30s 后失效 → 下一拍重试自愈。
    t = [20000.0]
    monkeypatch.setattr(cc.time, "time", lambda: t[0])
    cc._mark_negative_cache("k_short", ttl=30)
    t[0] = 20000 + 29
    assert cc._is_negatively_cached("k_short") is True     # 29 < 30
    t[0] = 20000 + 31
    assert cc._is_negatively_cached("k_short") is False    # 31 > 30


def test_short_and_long_coexist(monkeypatch):
    # 同表内短/长 TTL 各按自己的期限独立失效,互不影响。
    t = [30000.0]
    monkeypatch.setattr(cc.time, "time", lambda: t[0])
    cc._mark_negative_cache("k_long2")            # 300s
    cc._mark_negative_cache("k_short2", ttl=30)   # 30s
    t[0] = 30000 + 40   # 40s: short 已过期, long 仍有效
    assert cc._is_negatively_cached("k_short2") is False
    assert cc._is_negatively_cached("k_long2") is True


def test_remark_refreshes_ttl(monkeypatch):
    # 重新标记刷新起点与 ttl(短退避每次失败重置窗口)。
    t = [40000.0]
    monkeypatch.setattr(cc.time, "time", lambda: t[0])
    cc._mark_negative_cache("k_re", ttl=30)
    t[0] = 40000 + 25
    cc._mark_negative_cache("k_re", ttl=30)   # 25s 时重标 → 窗口从此刻重新算
    t[0] = 40000 + 50   # 距首标 50s 但距重标仅 25s
    assert cc._is_negatively_cached("k_re") is True
    t[0] = 40000 + 56   # 距重标 31s
    assert cc._is_negatively_cached("k_re") is False
