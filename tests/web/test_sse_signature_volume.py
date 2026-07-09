"""R5-#3: SSE 指纹须纳入末根成交量, 否则涨停量柱冻结。

一字涨停/跌停时 OHLC 恒定、成交量逐 tick 累积(prepend web-B1 只更 _data['v'][-1]);
volume 原不入 compute_signature→量柱更新恒被 dedup 吞→SSE 客户端量柱冻结整根 bar。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

from cl_app.services.sse_signature import compute_signature  # noqa: E402


def _bar(v_last):
    # 一字板: OHLC 全等且恒定, 仅末根量变化
    return {
        "t": [1, 2], "o": [1.0, 1.0], "h": [1.0, 1.0],
        "l": [1.0, 1.0], "c": [1.0, 1.0], "v": [100.0, v_last],
    }


def test_signature_changes_on_volume_only():
    # OHLC/根数/形态全同, 仅末根量 200→300 → 指纹必须变(否则量柱更新被吞)
    assert compute_signature(_bar(200.0)) != compute_signature(_bar(300.0))


def test_signature_stable_when_identical():
    assert compute_signature(_bar(200.0)) == compute_signature(_bar(200.0))