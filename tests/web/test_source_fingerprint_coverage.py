"""D3-M3 回归: source_fingerprint 必须覆盖 exchange 数据产出文件。

exchange 层对原始数据的解释(时区/周期合成/精度归一/去重)一改会改变图表 date/OHLC 值,
但原 source_fingerprint 只含 core/types/cl_utils/_lookback, 不含各数据源 exchange 文件 ->
这类修复(如本轮 tdx_us tz replace->localize)不失效旧图表缓存, 磁盘冷层持续下发旧时间戳。
纳入整个 exchange/*.py 后, 数据产出口径一改 -> 指纹变 -> chart_data/cl_object 缓存自动失效。
"""
from chanlun.tools.cache_identity import _fingerprint_files


def test_source_fingerprint_covers_exchange_dataoutput_files():
    names = {f.name for f in _fingerprint_files()}
    for f in (
        "exchange_cq.py", "exchange_usmart.py", "exchange_tdx_hk.py", "exchange_tdx_fx.py",
        "exchange_qmt.py", "exchange.py", "kline_precision.py",
    ):
        assert f in names, (
            f"{f} 未纳入 source_fingerprint 指纹 -> 数据源口径(时区/周期/精度)修复不失效图表缓存"
        )


def test_source_fingerprint_covers_strict_structure_sources():
    paths = {
        path.as_posix()
        for path in _fingerprint_files()
    }
    assert any(path.endswith("core/strict_structure/models.py") for path in paths)
    assert any(path.endswith("core/strict_structure/signals.py") for path in paths)
    assert any(path.endswith("cl_utils/strict_chart.py") for path in paths)
    assert any(path.endswith("cl_utils/strict_chart_runtime.py") for path in paths)
