"""list_chart_cache_codes 枚举正则须兼容含源码指纹段的缓存文件名。

chart_cache 文件名已演进为 v{N}_{source_fingerprint}_{market}_{CODE}_{freq}_{cfg}.pkl
(如 v36_05a12e39_a_SH_600519_5m_e436....pkl);旧正则 ^v\d+_{market}_... 匹配不到
指纹段 → 池枚举恒空 → --bt-pool-mode all/selector 全部 no symbols loaded(回归)。
"""
from chanlun.recursive_bt.engine.market_runtime import list_chart_cache_codes


def test_enumerates_fingerprinted_and_legacy_names(tmp_path):
    for name in (
        "v36_05a12e39_a_SH_600519_5m_e4367642b24cba1ed1c574b4655e97e0.pkl",  # 新:带指纹
        "v35_a_SZ_000858_5m_deadbeef.pkl",                                   # 旧:无指纹
        "v36_05a12e39_a_SH_600519_30m_e4367642b24cba1ed1c574b4655e97e0.pkl", # 其他周期不入
        "v36_05a12e39_us_QQQ_US_5m_cafebabe.pkl",                            # 其他市场不入
    ):
        (tmp_path / name).write_bytes(b"x")
    codes = list_chart_cache_codes("a", tmp_path, "5m")
    assert codes == ["SH.600519", "SZ.000858"]