"""LbQuotaTracker：月度 history kline symbol 配额追踪器单测。"""
import datetime
import json
import pathlib

import pytest


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    """让 tracker 落盘到 tmp_path，避免污染用户真实数据目录。"""
    monkeypatch.setattr("chanlun.config.get_data_path", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def tracker(temp_data_dir):
    from chanlun.exchange.lb_quota_tracker import LbQuotaTracker
    LbQuotaTracker._reset_singleton_for_test()
    return LbQuotaTracker.instance()


def test_add_symbol_persists_to_disk(tracker, temp_data_dir):
    tracker.add_symbol("QQQ.US")
    tracker.add_symbol("AAPL.US")

    files = list(temp_data_dir.glob("lb_quota_*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(data["symbols"]) == {"QQQ.US", "AAPL.US"}


def test_has_symbol_reads_from_persisted_set(tracker):
    tracker.add_symbol("QQQ.US")
    assert tracker.has_symbol("QQQ.US") is True
    assert tracker.has_symbol("UNKNOWN.US") is False


def test_count_reflects_unique_symbols(tracker):
    tracker.add_symbol("QQQ.US")
    tracker.add_symbol("QQQ.US")  # duplicate ignored
    tracker.add_symbol("AAPL.US")
    assert tracker.count() == 2


def test_is_exhausted_default_false(tracker):
    assert tracker.is_exhausted(limit=100) is False


def test_is_exhausted_after_limit_reached(tracker):
    for i in range(100):
        tracker.add_symbol(f"SYM{i}.US")
    assert tracker.is_exhausted(limit=100) is True
    assert tracker.is_exhausted(limit=200) is False


def test_mark_exhausted_overrides_count(tracker):
    """301607 触发时主动标记，即使 count 还没到 limit。"""
    tracker.add_symbol("QQQ.US")
    tracker.mark_exhausted()
    assert tracker.is_exhausted(limit=999) is True


def test_load_from_existing_file(temp_data_dir):
    """重启进程后能恢复上次落盘的 symbol 集合。"""
    today = datetime.date.today()
    fname = temp_data_dir / f"lb_quota_{today.year:04d}-{today.month:02d}.json"
    fname.write_text(json.dumps({
        "month": f"{today.year:04d}-{today.month:02d}",
        "symbols": ["QQQ.US", "AAPL.US"],
        "exhausted": False,
    }), encoding="utf-8")

    from chanlun.exchange.lb_quota_tracker import LbQuotaTracker
    LbQuotaTracker._reset_singleton_for_test()
    t = LbQuotaTracker.instance()
    assert t.count() == 2
    assert t.has_symbol("QQQ.US") is True


def test_month_change_resets_to_empty(temp_data_dir):
    """月份切换时不读上月文件。"""
    last_month = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
    fname = temp_data_dir / f"lb_quota_{last_month.year:04d}-{last_month.month:02d}.json"
    fname.write_text(json.dumps({
        "month": f"{last_month.year:04d}-{last_month.month:02d}",
        "symbols": ["UPLOADED_LAST_MONTH.US"] * 50,  # 上月数据
        "exhausted": True,
    }), encoding="utf-8")

    from chanlun.exchange.lb_quota_tracker import LbQuotaTracker
    LbQuotaTracker._reset_singleton_for_test()
    t = LbQuotaTracker.instance()
    assert t.count() == 0  # 当月文件不存在，从 0 开始
    assert t.is_exhausted(limit=100) is False
