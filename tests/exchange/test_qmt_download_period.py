"""QMT 合成周期 2m/10m/120m 的唯一基础周期是 1m。"""
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
