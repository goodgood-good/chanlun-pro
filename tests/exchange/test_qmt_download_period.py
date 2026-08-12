"""QMT 周期下载和时间边界契约测试。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from chanlun.exchange.exchange_qmt import ExchangeQMT
from chanlun.exchange.qmt_time_contract import qmt_exclusive_download_end


def test_qmt_declares_endpoint_labeled_klines():
    assert ExchangeQMT.kline_time_label == "end"


def test_qmt_synth_freqs_download_base_is_1m():
    ex = ExchangeQMT()
    for f in ("2m", "10m", "120m"):
        got = ex.download_frequency_map.get(f)
        assert got == "1m", (
            f"{f} 下载基础周期={got!r}, 应为 1m(与读取周期一致); 否则下载 1d 却读 1m, 冷标的返空/陈旧"
        )


def test_qmt_download_end_moves_past_inclusive_business_boundary():
    assert qmt_exclusive_download_end("2026-08-12 15:00:00") == (
        "20260812150001"
    )
    assert qmt_exclusive_download_end(
        datetime(2026, 8, 12, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    ) == "20260812150001"
