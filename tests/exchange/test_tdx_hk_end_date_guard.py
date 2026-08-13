"""锁定通达信港股范围参数不会错误触发空返回。"""

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_FILE = "src/chanlun/exchange/exchange_tdx_hk.py"


def test_tdx_hk_klines_guard_ignores_range_params():
    source = (_ROOT / _FILE).read_text(encoding="utf-8")
    assert "start_date is not None or end_date is not None" not in source, (
        "通达信港股 K 线守卫仍会因范围参数返回空值；Web 主加载传入 end_date 时将导致图表无数据"
    )
