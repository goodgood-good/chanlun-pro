# -*- coding: utf-8 -*-
"""R15-C1 (HIGH): tv_history 的 lazy HTF-MACD 自愈写回未持 chart_calc_locks(只持全局
cache_lock), 且 _patched=dict(cl_chart_data)(请求入口 T0 读到的陈旧快照)→ TOCTOU 下
若并发写者(SSE/revalidate 持 chart_calc_locks 完成的全量重算)在 T0 后写入新缠论,
该写回会用陈旧快照整体覆盖(新增K线/笔段/买卖点丢失, validated_at 重置压制 30s 内重算)。

修复(Option B, 避 locking 死锁): 补算基于缓存 entry 当前 data(_existing['data'], 锁内新读)
而非陈旧 cl_chart_data → 消除 stale-revert。单线程可测: entry 比本地 cl_chart_data 新时,
回写必须保留 entry 的新数据(只加 HTF), 不回退到本地陈旧快照。
"""
import cl_app.blueprints.tv as tv


def test_lazy_writeback_bases_on_current_entry_not_stale(monkeypatch):
    STALE = {"bars": ["b0"], "tag": "stale"}          # 请求入口 T0 读到的陈旧本地快照
    FRESH = {"bars": ["b0", "b1"], "tag": "fresh"}    # 并发写者写入缓存的新缠论

    written = {}
    monkeypatch.setattr(tv, "should_lazy_apply_higher_macd", lambda d, f, m: True)
    monkeypatch.setattr(
        tv, "_get_chart_cache_entry", lambda k: {"data": FRESH, "is_full_snapshot": True}
    )

    def _apply(patched, f, m, cfg):
        patched["higher_macd_hist"] = [1, 2, 3]  # 补 HTF
        return True

    monkeypatch.setattr(tv, "apply_higher_macd_to_chart_data", _apply)
    monkeypatch.setattr(
        tv, "_set_chart_cache_entry",
        lambda k, data, is_full_snapshot: written.update({"data": data, "is_full": is_full_snapshot}),
    )

    result = tv._lazy_writeback_htf("ckey", STALE, "5m", "a", {}, "SH.600000", "5")

    # 回写必须基于 FRESH(当前 entry), 不得回退到 STALE(TOCTOU 数据丢失)
    assert written["data"]["tag"] == "fresh", f"回写覆盖成陈旧快照: {written['data']}"
    assert written["data"]["bars"] == ["b0", "b1"]      # 并发写者的新 bar 保留
    assert written["data"]["higher_macd_hist"] == [1, 2, 3]  # HTF 补上
    assert result["tag"] == "fresh"


def test_lazy_writeback_falls_back_when_no_entry(monkeypatch):
    """回归: 缓存 entry 缺失(None)时回退用传入的 cl_chart_data, 不崩。"""
    LOCAL = {"bars": ["b0"], "tag": "local"}
    written = {}
    monkeypatch.setattr(tv, "should_lazy_apply_higher_macd", lambda d, f, m: True)
    monkeypatch.setattr(tv, "_get_chart_cache_entry", lambda k: None)
    monkeypatch.setattr(
        tv, "apply_higher_macd_to_chart_data",
        lambda patched, f, m, cfg: patched.__setitem__("higher_macd_hist", [9]) or True,
    )
    monkeypatch.setattr(
        tv, "_set_chart_cache_entry",
        lambda k, data, is_full_snapshot: written.update({"data": data}),
    )
    result = tv._lazy_writeback_htf("ckey", LOCAL, "5m", "a", {})
    assert written["data"]["tag"] == "local"
    assert result["higher_macd_hist"] == [9]