"""锁定 firstDataRequest 全量快照的「serve-stale + 后台重验证」语义(方向1)
以及交易时段感知的过期阈值(方向2)。

行为变更:此前 firstDataRequest 命中「过期」全量快照会判 MISS → 阻塞式重算
(实测 A 股 5m ex.klines 0.5-2s);现改为立即返回旧快照(秒显)并返回
needs_refresh=True,由调用方派后台重验证刷新缓存。范围(polling)路径不变。

evaluate_cache_for_tv_history 返回从 3 元组扩展为 4 元组:
    (is_hit, cached_data, miss_reason, needs_refresh)
"""
from cl_app.services.chart_cache import (
    _build_chart_cache_entry,
    evaluate_cache_for_tv_history,
)


def _full_entry(validated_at, min_t=1000, max_t=2000):
    return _build_chart_cache_entry(
        {"t": [min_t, max_t], "c": [1.0, 2.0]},
        is_full_snapshot=True,
        validated_at=validated_at,
    )


def test_first_request_fresh_snapshot_hits_without_refresh():
    now = 10_000.0
    entry = _full_entry(validated_at=now - 10)  # 10s 前,绝对新鲜
    is_hit, data, _reason, needs_refresh = evaluate_cache_for_tv_history(
        entry, 0, 0, is_range_request=False, market_is_trading=True, now=now,
    )
    assert is_hit is True
    assert data is not None
    assert needs_refresh is False


def test_first_request_moderately_stale_serves_stale_and_flags_refresh():
    now = 100_000.0
    # 盘中 10min: 300s(短阈值) < 600 < 1800s(上限) → 小幅过期 → serve-stale + 刷新
    entry = _full_entry(validated_at=now - 600)
    is_hit, data, _reason, needs_refresh = evaluate_cache_for_tv_history(
        entry, 0, 0, is_range_request=False, market_is_trading=True, now=now,
    )
    # 小幅过期:返回旧数据(秒显)+ 标记后台刷新(差几根 bar, 自愈快、用户基本无感)
    assert is_hit is True
    assert data is not None
    assert needs_refresh is True


def test_first_request_heavily_stale_trading_is_miss():
    # 重度过期(盘中 >30min 上限): 不再 serve-stale(会把几小时前的旧未完成笔发给前端,
    # 用户报"未完成笔滞后/该实仍虚"), 回退旧版阻塞重算保证新鲜。
    now = 100_000.0
    entry = _full_entry(validated_at=now - 7200)  # 2h 前 >> 盘中上限 1800s
    is_hit, _data, reason, needs_refresh = evaluate_cache_for_tv_history(
        entry, 0, 0, is_range_request=False, market_is_trading=True, now=now,
    )
    assert is_hit is False
    assert reason == "cache_stale_snapshot"
    assert needs_refresh is False


def test_trading_uses_short_threshold_closed_uses_long():
    now = 100_000.0
    # 盘中:400 > 300(短阈值) → 过期 → serve-stale + 刷新
    _, _, _, refresh_trading = evaluate_cache_for_tv_history(
        _full_entry(validated_at=now - 400), 0, 0,
        is_range_request=False, market_is_trading=True, now=now,
    )
    assert refresh_trading is True
    # 收盘:400 < 3600(长阈值) → 仍新鲜 → 不刷新(方向2:静态数据不反复重算)
    _, _, _, refresh_closed = evaluate_cache_for_tv_history(
        _full_entry(validated_at=now - 400), 0, 0,
        is_range_request=False, market_is_trading=False, now=now,
    )
    assert refresh_closed is False


def test_first_request_no_snapshot_is_miss():
    is_hit, _data, reason, needs_refresh = evaluate_cache_for_tv_history(
        None, 0, 0, is_range_request=False, market_is_trading=False, now=1.0,
    )
    assert is_hit is False
    assert reason == "cache_empty"
    assert needs_refresh is False


def test_first_request_partial_snapshot_is_miss():
    entry = _build_chart_cache_entry(
        {"t": [1, 2], "c": [1.0, 2.0]}, is_full_snapshot=False, validated_at=1.0,
    )
    is_hit, _data, reason, needs_refresh = evaluate_cache_for_tv_history(
        entry, 0, 0, is_range_request=False, market_is_trading=False, now=2.0,
    )
    assert is_hit is False
    assert reason == "cache_partial_snapshot"
    assert needs_refresh is False


def test_first_request_unknown_validated_at_is_miss():
    # 老格式 entry validated_at=0(无时间戳) → 时效未知 → 保守走 MISS-阻塞重算(而非
    # serve-stale 把可能很旧的快照原样发出), 一次重算后写入真实 validated_at。
    entry = _full_entry(validated_at=0)
    is_hit, _data, reason, needs_refresh = evaluate_cache_for_tv_history(
        entry, 0, 0, is_range_request=False, market_is_trading=False, now=5_000.0,
    )
    assert is_hit is False
    assert reason == "cache_stale_snapshot"
    assert needs_refresh is False


def test_first_request_closed_moderately_stale_serves_stale():
    # 收盘 2h: 3600s(长阈值) < 7200 < 86400s(收盘上限) → serve-stale(收盘数据静止, 无滞后)
    now = 1_000_000.0
    entry = _full_entry(validated_at=now - 7200)
    is_hit, data, _reason, needs_refresh = evaluate_cache_for_tv_history(
        entry, 0, 0, is_range_request=False, market_is_trading=False, now=now,
    )
    assert is_hit is True
    assert data is not None
    assert needs_refresh is True


def test_first_request_closed_multiday_stale_is_miss():
    # 收盘隔多日(>1 天上限): 缓存缺整段交易日 → MISS-阻塞重算保证补齐
    now = 1_000_000.0
    entry = _full_entry(validated_at=now - 3 * 86400)  # 3 天前
    is_hit, _data, reason, needs_refresh = evaluate_cache_for_tv_history(
        entry, 0, 0, is_range_request=False, market_is_trading=False, now=now,
    )
    assert is_hit is False
    assert reason == "cache_stale_snapshot"
    assert needs_refresh is False


def test_range_request_within_coverage_hits_no_refresh():
    now = 10_000.0
    entry = _full_entry(validated_at=now - 5, min_t=1000, max_t=5000)
    is_hit, _data, _reason, needs_refresh = evaluate_cache_for_tv_history(
        entry, 2000, 4000, is_range_request=True, market_is_trading=True, now=now,
    )
    assert is_hit is True
    assert needs_refresh is False


def test_range_request_tail_gap_still_miss_no_refresh():
    now = 10_000.0
    # validated_at 取一个相对真实时钟极久远的值 → 非 recently_validated(30s) → tail_gap miss
    entry = _full_entry(validated_at=1.0, min_t=1000, max_t=5000)
    is_hit, _data, reason, needs_refresh = evaluate_cache_for_tv_history(
        entry, 2000, 9000, is_range_request=True, market_is_trading=True, now=now,
    )
    assert is_hit is False
    assert reason == "cache_tail_gap"
    assert needs_refresh is False


# ── H1 force_refresh：前端断档 gap-reset 主动要求绕过缓存重算(阶段 E) ──

def test_force_refresh_bypasses_fresh_snapshot():
    # force_refresh=True: 即使快照绝对新鲜也判 MISS,强制重算(前端断档 gap-reset 主动要求刷新)。
    now = 10_000.0
    entry = _full_entry(validated_at=now - 10)  # 绝对新鲜,正常会 HIT
    is_hit, data, reason, needs_refresh = evaluate_cache_for_tv_history(
        entry, 0, 0, is_range_request=False, market_is_trading=True, now=now,
        force_refresh=True,
    )
    assert is_hit is False
    assert data is None
    assert reason == "cache_force_refresh"
    assert needs_refresh is False


def test_force_refresh_bypasses_range_coverage():
    # force_refresh 对 range 请求同样短路(即使覆盖完整也 MISS)。
    entry = _full_entry(validated_at=5_000.0, min_t=1000, max_t=5000)
    is_hit, _data, reason, _refresh = evaluate_cache_for_tv_history(
        entry, 1500, 4000, is_range_request=True, market_is_trading=True,
        force_refresh=True,
    )
    assert is_hit is False
    assert reason == "cache_force_refresh"


def test_force_refresh_default_false_preserves_hit():
    # 不传 force_refresh(默认 False): 新鲜快照仍 HIT,现有行为不回归。
    now = 10_000.0
    entry = _full_entry(validated_at=now - 10)
    is_hit, _data, _reason, _refresh = evaluate_cache_for_tv_history(
        entry, 0, 0, is_range_request=False, market_is_trading=True, now=now,
    )
    assert is_hit is True
