# -*- coding: utf-8 -*-
"""三处绕过 fdb.get_chart_cache 的裸 pickle.load 读者必须对腐坏/版本不兼容 pkl
返回 None(与 get_chart_cache "损坏返回 None" 契约一致),而非抛异常崩上层。

- engine.load_klines / market_runtime.load_chart_cache_klines: 读 chart_cache
  v* pkl;旧代码腐坏即 UnpicklingError 崩回测/实盘数据装载。
- portfolio.load_cached: 读 bt_data pkl;旧代码单只坏文件抛异常崩整个 universe
  批量载入(_load_bt_universe 全池循环)。所有调用点已容忍 None(is None / if d)。
(全维度优化 Commit3/健壮性)
"""


def test_load_klines_corrupt_returns_none(tmp_path, monkeypatch):
    import chanlun.recursive_bt.engine.engine as eng
    monkeypatch.setattr(eng, "CACHE_DIR", str(tmp_path))
    (tmp_path / "v1_TESTPFX_5m_0.pkl").write_bytes(b"\x80\x04broken-truncated")
    # 旧代码: pickle.load 抛 UnpicklingError; 修复后当作 miss 返回 None
    assert eng.load_klines("TESTPFX", "5m") is None


def test_load_klines_missing_returns_none(tmp_path, monkeypatch):
    import chanlun.recursive_bt.engine.engine as eng
    monkeypatch.setattr(eng, "CACHE_DIR", str(tmp_path))
    assert eng.load_klines("NOPE", "5m") is None


def test_load_chart_cache_klines_corrupt_returns_none(tmp_path, monkeypatch):
    import chanlun.recursive_bt.engine.market_runtime as mr
    bad = tmp_path / "v1_x_5m_0.pkl"
    bad.write_bytes(b"\x80\x04broken-truncated")
    monkeypatch.setattr(mr, "latest_chart_cache_file", lambda *a, **k: str(bad))
    assert mr.load_chart_cache_klines("a", "SH.600519", "5m", str(tmp_path)) is None


def test_load_cached_corrupt_returns_none(tmp_path, monkeypatch):
    import chanlun.recursive_bt.sim.portfolio as pf
    monkeypatch.setattr(pf, "BT_DATA", str(tmp_path))
    (tmp_path / "SH.600519.pkl").write_bytes(b"\x80\x04broken-truncated")
    # 旧代码: 单只坏 pkl 抛异常崩整批; 修复后当作缺失返回 None(与 not exists 同路径)
    assert pf.load_cached("SH.600519") is None