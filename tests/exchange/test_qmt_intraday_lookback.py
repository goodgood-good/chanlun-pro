"""QMT A 股分钟线回看窗口回归测试。"""

from datetime import datetime

from chanlun.exchange.exchange_qmt import ExchangeQMT
from chanlun.exchange import exchange_qmt


def test_qmt_one_minute_req_counts_uses_a_share_trading_sessions():
    ex = ExchangeQMT()

    start = datetime.strptime(
        ex.get_start_date_by_frequency("1m", req_counts=1_200),
        "%Y%m%d",
    )
    elapsed_days = (datetime.now().date() - start.date()).days

    # 1,200 根约等于 5 个 A 股交易日；保留既有 3 倍冗余并折算周末，
    # 至少应回看 21 个自然日，不能按 24 小时市场压缩成约 3 天。
    assert elapsed_days >= 21


def test_qmt_batch_prewarm_forwards_bounded_request_counts(monkeypatch):
    ex = ExchangeQMT()
    observed: list[tuple[str, int | None]] = []

    def start_date(frequency: str, req_counts: int | None = None) -> str:
        observed.append((frequency, req_counts))
        return "20260101"

    monkeypatch.setattr(ex, "get_start_date_by_frequency", start_date)
    monkeypatch.setattr(
        exchange_qmt.xtdata,
        "download_history_data2",
        lambda *_args, **_kwargs: None,
    )

    ex.prewarm_batch_download(
        ("SZ.000001",),
        ("30m", "5m"),
        req_counts_by_frequency={"30m": 600, "5m": 4_000},
    )

    assert observed == [("30m", 600), ("5m", 4_000)]
