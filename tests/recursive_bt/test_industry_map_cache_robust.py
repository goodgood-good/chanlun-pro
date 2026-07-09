# -*- coding: utf-8 -*-
"""build_industry_map 的 IND_MAP(行业映射)缓存 pkl 读/写健壮性。

R5-#5/#6 修了 industry._load_fund(FUND_DIR 下的 fund pkl)与 fetch 侧,但
build_industry_map 自身的 IND_MAP(industry_map.pkl)读写仍是裸 pickle:
- 读 pickle.load(open(IND_MAP,"rb")) 无 try/except 半写/腐坏缓存(如写盘被
  QMT 日重启 0xC0000409 中断)直接抛 UnpicklingError 崩季度池, 且 exists 跳过
  永不自愈。
- 写 pickle.dump(m, open(IND_MAP,"wb")) 非原子(open 即截断)中断留半写文件。
修复: 读侧 try/except 当作缺失并重建 + 写侧 tmp+os.replace 原子写。
(全维度优化 Commit2/健壮性)
"""
import pickle
import sys
import types

import pytest

import chanlun.recursive_bt.select.industry as industry_mod


def _make_fake_xtquant(sector="GICS1能源", codes=("600519.SH",)):
    """最小 xtquant 桩: 供 build_industry_map 重建路径调用, 无需真 QMT。"""
    xtdata = types.SimpleNamespace(
        download_sector_data=lambda: None,
        get_sector_list=lambda: [sector],
        get_stock_list_in_sector=lambda g: list(codes),
    )
    mod = types.ModuleType("xtquant")
    mod.xtdata = xtdata
    return mod


def test_corrupt_cache_rebuilds_not_raises(tmp_path, monkeypatch):
    """腐坏 IND_MAP: 旧代码 pickle.load 抛 UnpicklingError 崩; 修复后当作缺失重建。"""
    ind_map = tmp_path / "industry_map.pkl"
    ind_map.write_bytes(b"\x80\x04broken-truncated")
    monkeypatch.setattr(industry_mod, "IND_MAP", str(ind_map))
    monkeypatch.setitem(sys.modules, "xtquant", _make_fake_xtquant())

    m = industry_mod.build_industry_map(["SH.600519"])
    assert m == {"SH.600519": "GICS1能源"}
    with open(ind_map, "rb") as fh:
        assert pickle.load(fh) == {"SH.600519": "GICS1能源"}
    assert not list(tmp_path.glob("*.tmp"))


def test_valid_cache_hit_no_qmt_import(tmp_path, monkeypatch):
    """命中缓存(pool 有重叠)必须直接返回, 绝不触发 xtquant 导入。"""
    ind_map = tmp_path / "industry_map.pkl"
    good = {"SH.600519": "GICS1能源", "SZ.000001": "GICS1金融"}
    with open(ind_map, "wb") as fh:
        pickle.dump(good, fh)
    monkeypatch.setattr(industry_mod, "IND_MAP", str(ind_map))
    monkeypatch.setitem(sys.modules, "xtquant", None)

    assert industry_mod.build_industry_map(["SH.600519"]) == good


def test_atomic_write_preserves_old_on_failure(tmp_path, monkeypatch):
    """写 IND_MAP 中途抛异常时旧缓存必须完好(证明 tmp+os.replace 非 open(wb) 截断)。"""
    ind_map = tmp_path / "industry_map.pkl"
    old = {"SZ.999999": "GICS1旧"}
    with open(ind_map, "wb") as fh:
        pickle.dump(old, fh)
    monkeypatch.setattr(industry_mod, "IND_MAP", str(ind_map))
    monkeypatch.setitem(sys.modules, "xtquant", _make_fake_xtquant())

    class _BoomPickle:
        @staticmethod
        def load(fp):
            return pickle.load(fp)

        @staticmethod
        def dump(obj, fp):
            raise IOError("disk full mid-write")

    monkeypatch.setattr(industry_mod, "pickle", _BoomPickle)
    with pytest.raises(IOError):
        industry_mod.build_industry_map(["SH.600519"])

    monkeypatch.setattr(industry_mod, "pickle", pickle)
    with open(ind_map, "rb") as fh:
        assert pickle.load(fh) == old