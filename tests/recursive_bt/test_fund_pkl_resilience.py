"""R5-#5/#6: 腐坏 fund pkl 不得拖垮回测/选股 + 写侧原子防腐坏。

fundamentals.py 原 pickle.dump(open(wb)) 非原子(打开即截断), 进程中断(QMT 0xC0000409/
Ctrl+C/磁盘满)留半写文件; systems.attach_scores:52 与 industry._load_fund:52 直接
pickle.load 无 try/except→单只坏文件抛 EOFError 崩整批 walk-forward 回测/季度池, 且
skip-if-exists 使坏文件永不自愈。修复: 写侧原子(temp+os.replace) + 读侧 try/except 跳过并告警。
"""
import pickle

import chanlun.recursive_bt.select.industry as industry_mod
from chanlun.recursive_bt.engine.fundamentals import _atomic_dump


def test_load_fund_corrupt_pkl_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(industry_mod, "FUND_DIR", str(tmp_path))
    # 写半截/垃圾字节冒充 pkl
    (tmp_path / "SH.600000.pkl").write_bytes(b"\x80\x04broken-truncated")
    # 旧代码此处 pickle.load 抛 UnpicklingError/EOFError; 修复后当作缺失返回
    reports, total_share = industry_mod._load_fund("SH.600000")
    assert reports == []
    assert total_share is None


def test_load_fund_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(industry_mod, "FUND_DIR", str(tmp_path))
    reports, total_share = industry_mod._load_fund("SZ.000001")
    assert reports == [] and total_share is None


def test_load_fund_valid_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(industry_mod, "FUND_DIR", str(tmp_path))
    good = {"reports": [{"anntime": "2024-01-01", "rev_inc": 1.0, "np_inc": 2.0}],
            "total_share": 123.0}
    with open(tmp_path / "SH.600519.pkl", "wb") as fh:
        pickle.dump(good, fh)
    reports, total_share = industry_mod._load_fund("SH.600519")
    assert total_share == 123.0
    assert reports[0]["rev_inc"] == 1.0


def test_atomic_dump_roundtrip_and_no_tmp_leftover(tmp_path):
    path = str(tmp_path / "x.pkl")
    _atomic_dump({"a": 1, "b": [2, 3]}, path)
    with open(path, "rb") as fh:
        assert pickle.load(fh) == {"a": 1, "b": [2, 3]}
    # 成功后不得残留 .tmp
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_dump_overwrite_preserves_old_on_success(tmp_path):
    path = str(tmp_path / "y.pkl")
    _atomic_dump({"v": 1}, path)
    _atomic_dump({"v": 2}, path)
    with open(path, "rb") as fh:
        assert pickle.load(fh) == {"v": 2}