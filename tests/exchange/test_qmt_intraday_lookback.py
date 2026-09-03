"""QMT A 股分钟线回看窗口回归测试。"""

from contextlib import contextmanager
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

    result = ex.prewarm_batch_download(
        ("SZ.000001",),
        ("30m", "5m"),
        req_counts_by_frequency={"30m": 600, "5m": 4_000},
    )

    assert observed == [("30m", 600), ("5m", 4_000)]
    assert result == {
        "schema": "chanlun-qmt-batch-download-result",
        "cancelled": False,
        "successful_by_base": {"5m": ("SZ.000001",)},
        "failed_by_base": {"5m": ()},
    }


def test_qmt_batch_prewarm_reports_partial_chunk_failure(monkeypatch):
    """一个下载块失败不能抹掉其他块的本地可读资格。"""

    ex = ExchangeQMT()
    monkeypatch.setattr(ex, "get_start_date_by_frequency", lambda *_a, **_k: "20260101")
    calls: list[tuple[str, ...]] = []

    def download(codes, *_args, **_kwargs):
        calls.append(tuple(codes))
        if len(calls) == 1:
            raise RuntimeError("first chunk failed")

    monkeypatch.setattr(exchange_qmt.xtdata, "download_history_data2", download)
    result = ex.prewarm_batch_download(
        ("SZ.000001", "SZ.000002", "SZ.000003"),
        ("1m",),
        chunk_size=2,
        req_counts_by_frequency={"1m": 1_200},
    )

    assert result["cancelled"] is False
    assert result["successful_by_base"] == {"1m": ("SZ.000003",)}
    assert result["failed_by_base"] == {
        "1m": ("SZ.000001", "SZ.000002")
    }


def test_qmt_single_symbol_download_holds_shared_process_lane(monkeypatch):
    ex = ExchangeQMT()
    events: list[str] = []

    @contextmanager
    def shared_download_lane():
        events.append("shared_enter")
        try:
            yield
        finally:
            events.append("shared_exit")

    def download(**_kwargs):
        events.append("download")

    def read(**_kwargs):
        events.append("read")
        return {}

    monkeypatch.setattr(
        exchange_qmt,
        "_xtdata_download_interprocess_lock",
        shared_download_lane,
    )
    monkeypatch.setattr(exchange_qmt.xtdata, "download_history_data", download)
    monkeypatch.setattr(exchange_qmt.xtdata, "get_market_data", read)

    frame = ex.klines("SZ.000001", "5m", args={"req_counts": 10})

    assert frame.empty
    assert events == ["shared_enter", "download", "read", "shared_exit"]


def test_qmt_local_only_read_does_not_take_shared_download_lane(monkeypatch):
    ex = ExchangeQMT()
    subscriptions: list[tuple[object, ...]] = []

    @contextmanager
    def forbidden_download_lane():
        raise AssertionError("read-only QMT history must remain process-parallel")
        yield

    monkeypatch.setattr(
        exchange_qmt,
        "_xtdata_download_interprocess_lock",
        forbidden_download_lane,
    )
    monkeypatch.setattr(
        exchange_qmt.xtdata,
        "download_history_data",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("skip_download must not mutate QMT history")
        ),
    )
    monkeypatch.setattr(
        exchange_qmt.xtdata,
        "get_market_data",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        exchange_qmt.xtdata,
        "subscribe_quote2",
        lambda *args, callback, **_kwargs: (
            subscriptions.append(args),
            callback({}),
            7,
        )[-1],
    )

    frame = ex.klines(
        "SZ.000001",
        "5m",
        args={"req_counts": 10, "skip_download": True},
    )

    assert frame.empty
    assert subscriptions == [("000001.SZ", "5m")]


def test_qmt_local_only_intraday_subscription_is_reused(monkeypatch):
    ex = ExchangeQMT()
    subscriptions: list[dict[str, object]] = []
    unsubscribed: list[int] = []

    def subscribe(*args, callback, **kwargs):
        subscriptions.append({"args": args, **kwargs})
        callback({args[0]: []})
        return 11

    monkeypatch.setattr(exchange_qmt.xtdata, "subscribe_quote2", subscribe)
    monkeypatch.setattr(
        exchange_qmt.xtdata,
        "unsubscribe_quote",
        unsubscribed.append,
    )
    monkeypatch.setattr(exchange_qmt.xtdata, "get_market_data", lambda **_kwargs: {})
    monkeypatch.setattr(
        exchange_qmt.xtdata,
        "download_history_data",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("local subscription must not download history")
        ),
    )

    for _ in range(2):
        frame = ex.klines(
            "SH.600000",
            "1m",
            args={
                "req_counts": 10,
                "skip_download": True,
                "dividend_type": "front_ratio",
            },
        )
        assert frame.empty

    assert len(subscriptions) == 1
    assert subscriptions[0] == {
        "args": ("600000.SH", "1m"),
        "start_time": datetime.now().strftime("%Y%m%d"),
        "end_time": "",
        "count": -1,
        "dividend_type": "front_ratio",
    }

    assert ex.refresh_live_kline_subscription(
        "SH.600000",
        "1m",
        dividend_type="front_ratio",
    )
    assert len(subscriptions) == 2
    assert unsubscribed == [11]
