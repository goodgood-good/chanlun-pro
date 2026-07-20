"""D3-M1 回归: QMT 合成周期 2m/10m/120m 的下载基础周期必须是 1m(与读取周期一致)。

frequency_map 对 2m/10m/120m 无原生映射 -> 读取周期 fallback "1m" + convert 合成;
但 download_frequency_map 也无这三档 -> qmt_download_period fallback 落到 "1d"(不在 klines
的 ["1m","5m","15m","30m","60m"] 列表内)。下载日线却读 1 分钟: 冷标的(未预下载 1m)
get_market_data(1m) 返空 -> klines 返 empty; 或取到陈旧本地 1m 残留。
修复: download_frequency_map 显式补 2m/10m/120m -> "1m"。
"""
from chanlun.exchange.exchange_qmt import ExchangeQMT


def test_qmt_declares_endpoint_labeled_klines():
    assert ExchangeQMT.kline_time_label == "end"


def test_qmt_synth_freqs_download_base_is_1m():
    ex = ExchangeQMT()
    for f in ("2m", "10m", "120m"):
        got = ex.download_frequency_map.get(f)
        assert got == "1m", (
            f"{f} 下载基础周期={got!r}, 应为 1m(与读取周期一致); 否则下载 1d 却读 1m, 冷标的返空/陈旧"
        )
